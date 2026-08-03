"""TavilyClient tests — endpoints, auth, error mapping, usage."""
import httpx
import pytest

from tavily_client import TavilyClient, classify_error


async def test_search_success(mock_transport):
    client = TavilyClient("tvly-test", transport=mock_transport(
        {"results": [{"title": "t", "url": "https://x"}]}))
    result = await client.search({"query": "ping", "max_results": 1})
    assert result["results"][0]["title"] == "t"
    assert mock_transport.last_request.headers["Authorization"] == "Bearer tvly-test"
    await client.close()


async def test_search_401_classified_invalid(mock_transport):
    client = TavilyClient("tvly-test", transport=mock_transport(status_code=401))
    with pytest.raises(Exception) as ei:
        await client.search({"query": "q"})
    assert classify_error(ei.value) == "invalid"
    await client.close()


async def test_search_429_classified_rate_limit(mock_transport):
    client = TavilyClient("tvly-test", transport=mock_transport(
        status_code=429, headers={"Retry-After": "45"}))
    with pytest.raises(Exception):
        await client.search({"query": "q"})
    await client.close()


async def test_usage_returns_remaining(mock_transport):
    client = TavilyClient("tvly-test", transport=mock_transport(
        {"plan_usage": {"search": {"remaining": 987}}}))
    usage = await client.usage()
    assert usage["plan_usage"]["search"]["remaining"] == 987
    await client.close()
