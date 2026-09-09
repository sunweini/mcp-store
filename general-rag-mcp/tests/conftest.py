"""Shared test fixtures for general-rag-mcp tests.

Provides a FakeClient (in-memory RagClient stand-in) and a mock_rag client
driven by httpx MockTransport — no real general-rag API calls.
"""
import json

import httpx
import pytest

from rag_client import RagClient


class FakeClient:
    """In-memory RagClient stand-in for tool-layer unit tests.

    Test sets `search_result` / `namespaces_result` / `health_result` /
    `ingest_result`, or raises an error via `search_error`.
    """

    def __init__(self):
        self.search_result = {}
        self.namespaces_result = []
        self.health_result = {}
        self.ingest_result = {}
        self.ingest_path_result = {}
        self.golden_suggest_result = {}
        self.golden_add_result = {}
        self.delete_result = {}
        self.search_error = None
        self.search_calls = []
        self.ingest_path_error = None
        self.golden_suggest_error = None
        self.golden_add_error = None
        self.delete_error = None
        self.media_result = b""
        self.media_error = None

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        if self.search_error:
            raise self.search_error
        return self.search_result

    async def namespaces(self):
        # 对齐 RagClient.namespaces() 契约：返回 list（已解包 namespaces 字段）。
        return self.namespaces_result

    async def health(self):
        return self.health_result

    async def ingest(self, **kwargs):
        return self.ingest_result

    async def ingest_path(self, **kwargs):
        self.ingest_path_calls = getattr(self, "ingest_path_calls", [])
        self.ingest_path_calls.append(kwargs)
        if getattr(self, "ingest_path_error", None):
            raise self.ingest_path_error
        return self.ingest_path_result

    async def golden_suggest(self, **kwargs):
        self.golden_suggest_calls = getattr(self, "golden_suggest_calls", [])
        self.golden_suggest_calls.append(kwargs)
        if self.golden_suggest_error:
            raise self.golden_suggest_error
        return self.golden_suggest_result

    async def golden_add(self, **kwargs):
        self.golden_add_calls = getattr(self, "golden_add_calls", [])
        self.golden_add_calls.append(kwargs)
        if self.golden_add_error:
            raise self.golden_add_error
        return self.golden_add_result

    async def delete_document(self, **kwargs):
        self.delete_calls = getattr(self, "delete_calls", [])
        self.delete_calls.append(kwargs)
        if self.delete_error:
            raise self.delete_error
        return self.delete_result

    async def get_media(self, url):
        self.get_media_calls = getattr(self, "get_media_calls", [])
        self.get_media_calls.append(url)
        if self.media_error:
            raise self.media_error
        return self.media_result


def _make_response(status_code, body):
    return httpx.Response(
        status_code,
        json=body,
        headers={"content-type": "application/json"},
    )


@pytest.fixture
def fake_client():
    yield FakeClient()


@pytest.fixture
def mock_rag():
    """Create a RagClient with a queue of scripted httpx responses.

    Usage in tests:
        mock_rag.enqueue(status=200, body={...})
        mock_rag.enqueue(status=503, body={})   # will trigger backoff retry
        result = await mock_rag.search(...)
    """
    client = RagClient(base_url="http://mock-rag/api/v1", timeout=60)
    responses = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if responses:
            return responses.pop(0)
        return _make_response(200, {})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._responses = responses
    client.enqueue = lambda status, body: responses.append(_make_response(status, body))

    yield client
