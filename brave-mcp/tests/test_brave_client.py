"""BraveClient tests — endpoints, X-Subscription-Token auth, error mapping."""
import httpx
import pytest

from brave_client import BraveClient, BraveError, classify_error


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
    with pytest.raises(BraveError) as ei:
        await client.web_search({"q": "q"})
    assert ei.value.status_code == 401
    assert classify_error(ei.value) == "invalid"
    await client.close()


async def test_web_search_429_classified_rate_limit():
    client = BraveClient("BSA-test", transport=MockTransport(
        {}, status_code=429, headers={"Retry-After": "30"}))
    with pytest.raises(BraveError) as ei:
        await client.web_search({"q": "q"})
    assert ei.value.status_code == 429
    assert classify_error(ei.value) == "rate_limit"
    await client.close()


async def test_web_search_422_invalid_token_classified_invalid():
    # Brave 对无效 subscription token 实测返回 422 且 body detail 含
    # "The provided subscription token is invalid."（I-1 裁决：只按
    # body 文本匹配，不匹配裸码）。大小写不敏感匹配。
    client = BraveClient("BSA-test", transport=MockTransport(
        {"error": {"detail": "The provided subscription token is invalid."}},
        status_code=422))
    with pytest.raises(BraveError) as ei:
        await client.web_search({"q": "q"})
    assert ei.value.status_code == 422
    assert classify_error(ei.value) == "invalid"
    await client.close()


async def test_web_search_422_other_detail_not_mapped():
    # 422 还有参数错误语义（如非法 offset/count）——裸码匹配会误剔有效
    # key，必须不映射（I-1 裁决）
    client = BraveClient("BSA-test", transport=MockTransport(
        {"error": {"detail": "Invalid value for offset"}}, status_code=422))
    with pytest.raises(BraveError) as ei:
        await client.web_search({"q": "q"})
    assert classify_error(ei.value) is None
    await client.close()


async def test_local_search_endpoint():
    transport = MockTransport({"local": {"results": [{"title": "t"}]}})
    client = BraveClient("BSA-test", transport=transport)
    result = await client.local_search({"q": "pizza"})
    assert result["local"]["results"][0]["title"] == "t"
    assert transport.last_request.url.path == "/res/v1/local/search"
    await client.close()


async def test_proxy_passed_to_httpx_client(monkeypatch):
    # 生产网络 brave 直连不通,必须走内网代理。MockTransport 注入模式下
    # 代理不参与请求路径（httpx >= 0.27 允许 proxy 与 transport 共存），
    # 断言点放在构造函数：proxy 关键字透传给 httpx.AsyncClient。
    captured = {}
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        captured["proxy"] = kwargs.get("proxy")
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    client = BraveClient("BSA-test", proxy="http://10.16.12.12:7890")
    assert captured["proxy"] == "http://10.16.12.12:7890"
    await client.close()


async def test_proxy_none_means_direct(monkeypatch):
    # 未配置 SEARCH_PROXY 时传 None = 直连,httpx 不启用任何代理
    captured = {}
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        captured["proxy"] = kwargs.get("proxy")
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    client = BraveClient("BSA-test", proxy=None)
    assert captured["proxy"] is None
    await client.close()
