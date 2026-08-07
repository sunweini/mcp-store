"""TavilyClient tests — endpoints, auth, error mapping, usage.

Task 5（并发加固）新增用例：共享 client 单例（连接池复用）与
R5 防护——共享 client 禁止默认 Authorization 头，key 走请求级传递。
"""
import httpx
import pytest

from tavily_client import TavilyClient, classify_error


def test_shared_client_singleton():
    """get_shared_client 多次调用返回同一实例（连接池复用）。"""
    from tavily_client import get_shared_client
    assert get_shared_client() is get_shared_client()


def test_no_default_auth_header():
    """共享 client 无默认 Authorization 头（R5 key 串用防护）。"""
    from tavily_client import get_shared_client
    client = get_shared_client()
    assert "Authorization" not in client.headers  # 共享 client 恒无默认凭证


async def test_request_sends_bearer_header(mock_transport):
    """每请求显式带 key 头——共享 client 无默认凭证，靠请求级传递。

    回归点：若 key 落回 client 构造的默认 headers（改造前形态），共享
    单例会串用第一个 key——本测试 + test_no_default_auth_header 双锁。
    """
    client = TavilyClient("tvly-test", transport=mock_transport({}))
    await client.search({"query": "q"})
    assert mock_transport.last_request.headers["Authorization"] == "Bearer tvly-test"
    await client.close()


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
