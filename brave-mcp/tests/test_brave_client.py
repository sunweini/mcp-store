"""BraveClient tests — endpoints, X-Subscription-Token auth, error mapping.

Task 5（并发加固）新增用例：共享 client 单例（连接池复用）与 R5
防护——共享 client 无默认凭证头，key 走请求级传递。原「proxy 经
BraveClient 透传」两测试改为断言共享 client 的代理形态（proxy 归
共享 client 所有，构造时从 SEARCH_PROXY env 读）。
"""
import httpx
import pytest

from brave_client import BraveClient, BraveError, classify_error, get_shared_client


def test_shared_client_singleton():
    """get_shared_client 多次调用返回同一实例（连接池复用）。"""
    assert get_shared_client() is get_shared_client()


def test_no_default_auth_header():
    """共享 client 无默认 X-Subscription-Token 头（R5 key 串用防护）。"""
    client = get_shared_client()
    assert "X-Subscription-Token" not in client.headers  # 共享 client 恒无默认凭证


async def test_request_sends_key_header():
    """每请求显式带 key 头——共享 client 无默认凭证，靠请求级传递。

    回归点：若 key 落回 client 构造的默认 headers（改造前形态），共享
    单例会串用第一个 key——本测试 + test_no_default_auth_header 双锁。
    """
    transport = MockTransport({"web": {"results": []}})
    client = BraveClient("BSA-test", transport=transport)
    await client.web_search({"q": "hello"})
    assert transport.last_request.headers["X-Subscription-Token"] == "BSA-test"
    await client.close()


async def test_shared_client_honors_search_proxy_env(monkeypatch):
    """共享 client 按 SEARCH_PROXY 走代理（brave 生产必须走内网代理）。

    回归点：proxy 归共享 client 所有（构造时从 env 读）——若代理回到
    BraveClient 逐请求传，共享 client 会直连（生产网络必失败）。
    断言点放在 get_shared_client 构造处：捕获 httpx.AsyncClient 的
    proxy 关键字。重置模块单例避免跨测试污染（singleton 语义下
    只能测"首次构造"路径）。
    """
    import brave_client
    monkeypatch.setenv("SEARCH_PROXY", "http://10.16.12.12:7890")
    captured = {}
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        captured["proxy"] = kwargs.get("proxy")
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    brave_client._shared_client = None  # 重置单例，按当前 env 重建
    client = brave_client.get_shared_client()
    assert captured["proxy"] == "http://10.16.12.12:7890"


async def test_shared_client_no_proxy_when_env_empty(monkeypatch):
    """未配置 SEARCH_PROXY 时共享 client 直连（proxy=None，httpx 不启用代理）。"""
    import brave_client
    monkeypatch.delenv("SEARCH_PROXY", raising=False)
    captured = {}
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):
        captured["proxy"] = kwargs.get("proxy")
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
    brave_client._shared_client = None
    client = brave_client.get_shared_client()
    assert captured["proxy"] is None


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
