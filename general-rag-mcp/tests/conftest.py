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
        self.search_error = None
        self.search_calls = []

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
