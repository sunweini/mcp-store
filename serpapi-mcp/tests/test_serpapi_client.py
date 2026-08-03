"""SerpapiClient tests — engine param, api_key, error mapping (incl. account limit)."""
import json

import httpx
import pytest

from serpapi_client import SerpapiClient, SerpapiError, classify_error


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
