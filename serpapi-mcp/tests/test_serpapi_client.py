"""SerpapiClient tests — engine param, api_key, error mapping (incl. account limit).

Task 5（并发加固）新增用例：共享 client 单例（连接池复用）与 R5
防护——共享 client 无默认凭证（serpapi 的 api_key 是 URL query 参数，
不走 header；断言共享 client 无默认 Authorization 头 + 每请求 key 走
query）。
"""
import json

import httpx
import pytest

from serpapi_client import SerpapiClient, SerpapiError, classify_error


def test_shared_client_singleton():
    """get_shared_client 多次调用返回同一实例（连接池复用）。"""
    from serpapi_client import get_shared_client
    assert get_shared_client() is get_shared_client()


def test_no_default_auth_header():
    """共享 client 无默认凭证头（R5 key 串用防护）。

    serpapi 的 api_key 走 URL query 而非 header——共享 client 无
    Authorization/默认 headers，key 泄漏风险只存在于 query 路径
    （由 httpx logger WARNING 防线 + 日志不记完整 URL 处理）。
    """
    from serpapi_client import get_shared_client
    client = get_shared_client()
    assert "Authorization" not in client.headers  # 共享 client 恒无默认凭证


async def test_request_sends_api_key_in_query():
    """每请求 key 走 query 参数——共享 client 无默认凭证，靠请求级传递。

    回归点：若 key 落回 client 构造的默认（改造前形态），共享单例会
    串用第一个 key；serpapi 形态是 query 而非 header，断言点在
    last_request.url.params["api_key"]。
    """
    transport = MockTransport({"organic_results": []})
    client = SerpapiClient("serp-test", transport=transport)
    await client.search("google", {"q": "hello"})
    assert transport.last_request.url.params["api_key"] == "serp-test"
    await client.close()


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload, status_code=200, headers=None):
        self._payload, self._status_code = payload, status_code
        self._headers = headers or {}
        self.last_request = None

    async def handle_async_request(self, request):
        self.last_request = request
        return httpx.Response(self._status_code, json=self._payload,
                              headers=self._headers, request=request)


async def test_google_engine_and_api_key_param():
    transport = MockTransport({"organic_results": [{"title": "t", "link": "https://x"}]})
    client = SerpapiClient("serp-test", transport=transport)
    result = await client.search("google", {"q": "hello", "num": 5})
    assert result["organic_results"][0]["title"] == "t"
    assert transport.last_request.url.params["engine"] == "google"
    assert transport.last_request.url.params["api_key"] == "serp-test"
    await client.close()


async def test_ebay_engine():
    transport = MockTransport({"shopping_results": [{"title": "t"}]})
    client = SerpapiClient("serp-test", transport=transport)
    result = await client.search("ebay", {"_nkw": "laptop", "ebay_domain": "ebay.com"})
    assert "shopping_results" in result
    assert transport.last_request.url.params["engine"] == "ebay"
    await client.close()


async def test_account_limit_classified_exhausted():
    body = {"error": "Account has exceeded quota, for more info visit https://serpapi.com/pricing"}
    client = SerpapiClient("serp-test", transport=MockTransport(body, status_code=200))
    with pytest.raises(SerpapiError):
        await client.search("google", {"q": "q"})
    assert classify_error(Exception(), 200, json.dumps(body)) == "exhausted"
    await client.close()


async def test_401_classified_invalid():
    client = SerpapiClient("serp-test", transport=MockTransport({}, status_code=401))
    with pytest.raises(SerpapiError) as ei:
        await client.search("google", {"q": "q"})
    assert ei.value.status_code == 401
    assert classify_error(ei.value, 401, "") == "invalid"
    await client.close()


async def test_429_classified_rate_limit():
    client = SerpapiClient("serp-test", transport=MockTransport(
        {}, status_code=429, headers={"Retry-After": "60"}))
    with pytest.raises(SerpapiError) as ei:
        await client.search("google", {"q": "q"})
    assert ei.value.status_code == 429
    assert classify_error(ei.value, 429, "") == "rate_limit"
    await client.close()


async def test_search_does_not_mutate_caller_params():
    """brief 明示 params = dict(params) 拷贝——调用方 dict 不得被污染。

    回归点：若省掉拷贝直接改原 dict，engine/api_key 会残留到调用方的
    params 上（如工具层复用同一 dict 重试时参数错乱）。
    """
    transport = MockTransport({"organic_results": []})
    client = SerpapiClient("serp-test", transport=transport)
    params = {"q": "hello", "num": 5}
    await client.search("google", params)
    assert params == {"q": "hello", "num": 5}  # 无 engine/api_key 残留
    await client.close()


async def test_429_with_quota_body_wins_over_status():
    """429 + body 含 quota 关键词时仍归 RATE_LIMIT（先判状态码）。

    classify_error 顺序：401 → 429 → body 关键词。serpapi 欠费实测
    返回 200 + error body，429 的 body 通常不含 quota 关键词，但顺序
    语义必须稳定：状态码优先于 body 文本。
    """
    assert classify_error(Exception(), 429, "account has exceeded quota") == "rate_limit"
