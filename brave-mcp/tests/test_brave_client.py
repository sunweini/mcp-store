"""BraveClient tests — endpoints, X-Subscription-Token auth, error mapping."""
import httpx
import pytest

from brave_client import BraveClient, classify_error


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload, status_code=200, headers=None):
        self._payload, self._status_code = payload, status_code
        self._headers = headers or {}
        self.last_request = None

    async def handle_async_request(self, request):
        self.last_request = request
        return httpx.Response(self._status_code, json=self._payload,
                              headers=self._headers, request=request)


async def test_web_search_success_and_auth_header():
    transport = MockTransport({"web": {"results": [{"title": "t", "url": "https://x"}]}})
    client = BraveClient("BSA-test", transport=transport)
    result = await client.web_search({"q": "hello", "count": 5})
    assert result["web"]["results"][0]["title"] == "t"
    assert transport.last_request.headers["X-Subscription-Token"] == "BSA-test"
    assert transport.last_request.url.path == "/res/v1/web/search"
    await client.close()


async def test_web_search_401_classified_invalid():
    client = BraveClient("BSA-test", transport=MockTransport({}, status_code=401))
    with pytest.raises(Exception):
        await client.web_search({"q": "q"})
    assert classify_error(Exception(), 401) == "invalid"
    await client.close()


async def test_web_search_429_classified_rate_limit():
    client = BraveClient("BSA-test", transport=MockTransport(
        {}, status_code=429, headers={"Retry-After": "30"}))
    with pytest.raises(Exception):
        await client.web_search({"q": "q"})
    assert classify_error(Exception(), 429) == "rate_limit"
    await client.close()


async def test_local_search_endpoint():
    transport = MockTransport({"local": {"results": [{"title": "t"}]}})
    client = BraveClient("BSA-test", transport=transport)
    result = await client.local_search({"q": "pizza"})
    assert result["local"]["results"][0]["title"] == "t"
    assert transport.last_request.url.path == "/res/v1/local/search"
    await client.close()
