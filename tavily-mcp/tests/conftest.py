"""Shared httpx mock transport + pool fixture for tavily tests."""
import json
import pytest
import httpx

from key_pool import KeyPool
from tavily_client import TavilyClient


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self._status_code = status_code
        self._headers = headers or {}
        self.last_request = None

    async def handle_async_request(self, request):
        self.last_request = request
        return httpx.Response(
            self._status_code, json=self._payload,
            headers=self._headers, request=request)


class MockTransportFactory:
    """Callable factory that also exposes the last created transport's
    request. Tests do `mock_transport(payload)` to build a transport and
    `mock_transport.last_request` to inspect what was sent — the brief's
    tests rely on the fixture itself being callable AND observable.
    """

    def __init__(self):
        self._transport = None

    def __call__(self, payload=None, status_code=200, headers=None):
        self._transport = MockTransport(payload or {}, status_code, headers)
        return self._transport

    @property
    def last_request(self):
        return self._transport.last_request if self._transport else None


@pytest.fixture
def mock_transport():
    return MockTransportFactory()
