"""Tool layer tests — parameter passthrough, pool integration, errors.

工具是模块级函数(pool 显式传入),测试用 FakePool + FakeClient 驱动,
不涉及真实 HTTP 或 Redis。client_factory 注入点避免依赖私有 _get。
"""
from key_pool import ErrorKind
from serpapi_client import SerpapiError
from tools.search import (
    serpapi_google, serpapi_bing, serpapi_baidu,
    serpapi_duckduckgo, serpapi_ebay,
)


class FakeClient:
    """Scriptable fake SerpapiClient: records calls, fails per-engine."""

    def __init__(self, key, timeout=5.0, fail=None, fail_status=401, fail_body="",
                 fail_exc=None):
        self.key = key
        self.timeout = timeout
        self.fail = fail
        self.fail_status = fail_status
        self.fail_body = fail_body
        self.fail_exc = fail_exc  # 注入任意异常（模拟 httpx 网络异常）
        self.calls = []
        self.closed = 0

    async def close(self):
        # 镜像真实 SerpapiClient.close()（异步关闭 httpx.AsyncClient）。
        # 回归点：关闭方法名是 close 而非 aclose——写错则 _once 的
        # getattr 恒为 None、连接泄漏依旧且无测试能发现
        self.closed += 1

    async def search(self, engine, params):
        self.calls.append((engine, dict(params)))
        if self.fail_exc is not None:
            raise self.fail_exc
        if self.fail == engine:
            raise SerpapiError(self.fail_status, self.fail_body or "scripted failure")
        return {"engine": engine, "params": dict(params)}


class FakePool:
    """In-memory KeyPool stand-in.

    next_key 跳过 invalid/exhausted;on_error 按 kind 标记 key,
    使 failover 场景 next_key 自然轮换到下一个可用 key。
    """

    def __init__(self, records=None):
        if records is None:
            records = {
                "k1": {"key_id": "k1", "key": "SERP-k1", "provider": "serpapi",
                       "monthly_quota": 100, "status": "active", "remaining": 90},
            }
        self._records = records
        self.errors = []
        self.successes = []

    async def next_key(self):
        for rec in self._records.values():
            if rec.get("status") not in ("invalid", "exhausted"):
                return rec
        return None

    async def on_success(self, key_id, remaining=None):
        self.successes.append(key_id)

    async def on_error(self, key_id, kind, retry_after=None):
        self.errors.append((key_id, kind))
        if kind in (ErrorKind.INVALID, ErrorKind.EXHAUSTED):
            self._records[key_id]["status"] = kind.value


def _client_factory(made, fail=None, fail_status=401, fail_body="", fail_once=False):
    """Build a client_factory; every created FakeClient appended to `made`.

    fail: engine 名（该 engine 所有调用失败）。fail_once: 仅第一个
    client 失败（failover 成功场景）；需与 fail 联用。
    """
    def _make(key, timeout):
        client = FakeClient(key, timeout=timeout,
                            fail=(fail if not fail_once or not made else None),
                            fail_status=fail_status, fail_body=fail_body)
        made.append(client)
        return client
    return _make


def _rec(key_id, key, **over):
    rec = {"key_id": key_id, "key": key, "provider": "serpapi",
           "monthly_quota": 100, "status": "active", "remaining": 90}
    rec.update(over)
    return rec


# ── 参数透传（5 engines）─────────────────────────────────────────────────────


async def test_google_passes_params():
    pool = FakePool()
    made = []
    result = await serpapi_google(
        "hello world", gl="us", hl="en", num=5, start=10, pool=pool,
        client_factory=_client_factory(made))
    assert result["status"] == "ok"
    engine, params = made[0].calls[0]
    assert engine == "google"
    assert params == {"q": "hello world", "gl": "us", "hl": "en", "num": 5, "start": 10}
    assert made[0].timeout == 5.0
    assert pool.successes == ["k1"]
    assert made[0].closed == 1  # 每个 client 用完即关（close 非 aclose）


async def test_bing_passes_params():
    made = []
    result = await serpapi_bing(
        "q", gl="us", hl="en", cc="us", count=8, pool=FakePool(),
        client_factory=_client_factory(made))
    assert result["status"] == "ok"
    engine, params = made[0].calls[0]
    assert engine == "bing"
    assert params == {"q": "q", "gl": "us", "hl": "en", "cc": "us", "count": 8}


async def test_bing_defaults_count():
    made = []
    await serpapi_bing("q", pool=FakePool(), client_factory=_client_factory(made))
    engine, params = made[0].calls[0]
    assert engine == "bing"
    assert params == {"q": "q", "count": 10}


async def test_baidu_passes_params():
    made = []
    result = await serpapi_baidu("q", cti="2", page_num=3, pool=FakePool(),
                                 client_factory=_client_factory(made))
    assert result["status"] == "ok"
    engine, params = made[0].calls[0]
    assert engine == "baidu"
    assert params == {"q": "q", "cti": "2", "page_num": 3}


async def test_duckduckgo_passes_params():
    made = []
    result = await serpapi_duckduckgo("q", kl="us-en", pool=FakePool(),
                                      client_factory=_client_factory(made))
    assert result["status"] == "ok"
    engine, params = made[0].calls[0]
    assert engine == "duckduckgo"
    assert params == {"q": "q", "kl": "us-en"}


async def test_ebay_passes_params():
    made = []
    result = await serpapi_ebay("laptop", ebay_domain="ebay.com", pool=FakePool(),
                                client_factory=_client_factory(made))
    assert result["status"] == "ok"
    engine, params = made[0].calls[0]
    assert engine == "ebay"
    assert params == {"_nkw": "laptop", "ebay_domain": "ebay.com"}


async def test_ebay_default_domain():
    made = []
    await serpapi_ebay("laptop", pool=FakePool(), client_factory=_client_factory(made))
    engine, params = made[0].calls[0]
    assert params == {"_nkw": "laptop", "ebay_domain": "ebay.com"}


async def test_params_not_mutated_by_call_with_pool():
    """工具层传给 client 的 params 不得被 SerpapiClient 污染（dict 拷贝）。

    断言的是 client 收到的 params 与工具层构建的一致——无 engine/api_key
    残留（SerpapiClient.search 内部 dict(params) 拷贝的回归点）。
    """
    made = []
    await serpapi_google("q", num=5, pool=FakePool(), client_factory=_client_factory(made))
    assert made[0].calls[0][1] == {"q": "q", "num": 5, "start": 0}  # 无 engine/api_key 残留


# ── 校验 / 无 key ────────────────────────────────────────────────────────────


async def test_empty_query_returns_error():
    result = await serpapi_google("", pool=FakePool())
    assert result["status"] == "error"
    result = await serpapi_ebay("", pool=FakePool())
    assert result["status"] == "error"


async def test_no_keys_returns_error():
    pool = FakePool(records={})
    result = await serpapi_google("q", pool=pool)
    assert result["status"] == "error"
    assert "不可用" in result["message"]
    assert not pool.successes


# ── pool 集成（真实 KeyPool + FakeRedis）──────────────────────────────────────


async def test_google_with_fixture_pool(fake_pool):
    """真实 KeyPool（FakeRedis 驱动）集成：成功记账写回 Redis。"""
    made = []
    result = await serpapi_google("q", pool=fake_pool, client_factory=_client_factory(made))
    assert result["status"] == "ok"
    assert made[0].key == "SERP-a"  # next_key 选剩余最高的 k1
    # on_success 未传 remaining，KeyPool 保留旧值 90——断言防回归
    # 「成功路径误改 remaining/配额」
    assert fake_pool._records["k1"]["remaining"] == 90
    assert fake_pool._records["k1"]["last_used_at"] is not None
    assert fake_pool._records["k1"]["last_error"] is None


# ── failover / 重试策略 ──────────────────────────────────────────────────────


async def test_failover_retries_next_key():
    """k1 401 → 剔除 → 重试 k2 成功（幂等查询才允许的 failover）。"""
    pool = FakePool(records={
        "k1": _rec("k1", "SERP-k1"),
        "k2": _rec("k2", "SERP-k2"),
    })
    made = []
    result = await serpapi_google(
        "q", pool=pool, client_factory=_client_factory(made, fail="google", fail_once=True))
    assert result["status"] == "ok"
    assert [c.key for c in made] == ["SERP-k1", "SERP-k2"]  # 换 key 重试
    assert pool.errors == [("k1", ErrorKind.INVALID)]
    assert pool.successes == ["k2"]


async def test_failover_second_key_failure_returns_error():
    pool = FakePool(records={"k1": _rec("k1", "SERP-k1"), "k2": _rec("k2", "SERP-k2")})
    made = []
    result = await serpapi_google(
        "q", pool=pool, client_factory=_client_factory(made, fail="google"))
    # 两个 key 都 401 → error,且两个 key 都被标记剔除
    assert result["status"] == "error"
    assert pool.errors == [("k1", ErrorKind.INVALID), ("k2", ErrorKind.INVALID)]
    assert made[0].key == "SERP-k1" and made[1].key == "SERP-k2"


async def test_rate_limit_does_not_retry_same_key():
    pool = FakePool(records={"k1": _rec("k1", "SERP-k1")})
    made = []
    # 429 → RATE_LIMIT;重试要求不同 key_id,单 key 池不重试
    result = await serpapi_google(
        "q", pool=pool,
        client_factory=_client_factory(made, fail="google", fail_status=429))
    assert result["status"] == "error"
    assert pool.errors == [("k1", ErrorKind.RATE_LIMIT)]
    assert len(made) == 1


async def test_quota_exhausted_marks_key_exhausted():
    """200 + error body（欠费）→ EXHAUSTED 剔除；failover 到 k2。

    回归点：serpapi 欠费返回 200 而非 4xx，若工具层 classify_error
    漏传 body_text，EXHAUSTED 检测失效（误归 EXHAUSTED 兜底——本测试
    断言精确 kind 与剔除行为）。
    """
    body = {"error": "Account has exceeded quota, for more info visit https://serpapi.com/pricing"}
    pool = FakePool(records={
        "k1": _rec("k1", "SERP-k1"),
        "k2": _rec("k2", "SERP-k2"),
    })
    made = []
    result = await serpapi_google(
        "q", pool=pool,
        client_factory=_client_factory(made, fail="google", fail_status=200,
                                       fail_body=body["error"], fail_once=True))
    assert result["status"] == "ok"
    assert [c.key for c in made] == ["SERP-k1", "SERP-k2"]
    assert pool.errors == [("k1", ErrorKind.EXHAUSTED)]
    assert pool.successes == ["k2"]


async def test_quota_exhausted_single_key_returns_error():
    pool = FakePool(records={"k1": _rec("k1", "SERP-k1")})
    made = []
    result = await serpapi_google(
        "q", pool=pool,
        client_factory=_client_factory(
            made, fail="google", fail_status=200,
            fail_body="Your account has insufficient credits"))
    assert result["status"] == "error"
    assert pool.errors == [("k1", ErrorKind.EXHAUSTED)]
    assert len(made) == 1


async def test_clients_closed_on_success_fail_and_failover():
    """每个创建的 client 都必须关闭（N1 回归：关闭方法名是 close 不是
    aclose——tavily 上轮错写 aclose 导致 getattr 恒为 None，连接泄漏无感知）。"""
    # 成功路径
    made = []
    await serpapi_google("q", pool=FakePool(), client_factory=_client_factory(made))
    assert made[-1].closed == 1

    # 失败路径（k1 401 后无第二 key → 不再重试）
    pool2 = FakePool(records={"k1": _rec("k1", "SERP-k1")})
    made2 = []
    await serpapi_google("q", pool=pool2,
                         client_factory=_client_factory(made2, fail="google"))
    assert made2[0].closed == 1

    # failover 路径（k1 失败 → k2 成功，两个 client 都要关）
    pool3 = FakePool(records={"k1": _rec("k1", "SERP-k1"), "k2": _rec("k2", "SERP-k2")})
    made3 = []
    await serpapi_google("q", pool=pool3,
                         client_factory=_client_factory(made3, fail="google",
                                                        fail_once=True))
    assert [c.closed for c in made3] == [1, 1]


# ── 错误消息防泄漏（评审 I-1 回归）────────────────────────────────────────────


class _LeakyURLException(Exception):
    """模拟 httpx 网络异常：repr/str 含完整请求 URL（带 api_key query）。

    httpx.ConnectError/TimeoutError 等的 message 都带请求 URL——
    serpapi 的 api_key 在 query 里，最终失败消息若 str(exc) 会把
    明文 key 带进工具返回体。真实异常示例：
    "connect error: connect ECONNREFUSED 127.0.0.1:1
     https://serpapi.com/search.json?...&api_key=tvly-xxx"
    """

    def __str__(self):
        return ("connect error: ECONNREFUSED "
                "https://serpapi.com/search.json?engine=google&q=x&api_key=SECRET-KEY-123")


async def test_network_error_message_does_not_leak_api_key():
    """网络异常（无 status_code 属性）→ 泛化消息，不得含 URL/key。"""
    pool = FakePool(records={"k1": _rec("k1", "SERP-k1")})
    made = []

    def _make(key, timeout):
        client = FakeClient(key, timeout=timeout, fail_exc=_LeakyURLException())
        made.append(client)
        return client

    result = await serpapi_google("q", pool=pool, client_factory=_make)
    assert result["status"] == "error"
    # 消息不得泄漏 key / URL（httpx 异常 str 含完整 query URL）
    assert "SECRET-KEY-123" not in result["message"]
    assert "serpapi.com" not in result["message"]
    assert "SECRET-KEY-123" not in str(result)
    # 泛化消息保留排障信息
    assert "网络/超时" in result["message"]


async def test_http_error_message_has_status_and_truncated_body_only():
    """业务异常（SerpapiError）→ 消息只含 status + 截断 body（评审 I-1）。

    body 是响应体文本（SerpAPI 实测不回显 api_key，仅含错误说明），
    非请求 URL；消息格式锁定为 "serpapi error <status>: <body>"。
    """
    pool = FakePool(records={"k1": _rec("k1", "SERP-k1")})
    made = []
    result = await serpapi_google(
        "q", pool=pool,
        client_factory=_client_factory(made, fail="google", fail_status=401,
                                       fail_body="Unauthorized: missing or invalid API key."))
    assert result["status"] == "error"
    # 消息只含 status + body，不含请求 URL（api_key 在 URL query 里）
    assert result["message"].startswith("serpapi error 401: ")
    assert "Unauthorized" in result["message"]
    assert "SERP-k1" not in result["message"]
    assert "serpapi.com" not in result["message"]


# ── MCP 注册 ─────────────────────────────────────────────────────────────────


async def test_register_exposes_five_readonly_tools():
    from fastmcp import FastMCP
    from tools.search import register
    mcp = FastMCP("test")
    register(mcp, lambda: FakePool())
    tools = await mcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == ["serpapi_baidu", "serpapi_bing", "serpapi_duckduckgo",
                     "serpapi_ebay", "serpapi_google"]
    for t in tools:
        assert t.annotations.read_only_hint is True
        assert t.description  # docstring 生成 description


async def test_register_schema_exposes_params_not_pool():
    from fastmcp import FastMCP
    from tools.search import register
    mcp = FastMCP("test")
    register(mcp, lambda: FakePool())
    tools = {t.name: t for t in await mcp.list_tools()}
    google_props = (tools["serpapi_google"].parameters or {}).get("properties", {})
    for p in ("query", "gl", "hl", "num", "start"):
        assert p in google_props
    assert "pool" not in google_props  # 注入参数不出现在 schema
    assert "client_factory" not in google_props
    ebay_props = (tools["serpapi_ebay"].parameters or {}).get("properties", {})
    for p in ("_nkw", "ebay_domain"):
        assert p in ebay_props
    assert "pool" not in ebay_props


async def test_register_tools_aggregator():
    from fastmcp import FastMCP
    from tools import register_tools
    mcp = FastMCP("test")
    register_tools(mcp, lambda: FakePool())
    names = {t.name for t in await mcp.list_tools()}
    assert names == {"serpapi_google", "serpapi_bing", "serpapi_baidu",
                     "serpapi_duckduckgo", "serpapi_ebay"}


# ── metrics wrapper ─────────────────────────────────────────────────────────


async def test_metrics_wrapper_records_status(monkeypatch):
    from tools import _metrics_wrapper

    class FakeMetric:
        def __init__(self):
            self.adds = []
            self.records = []

        def add(self, amount, attributes=None):
            self.adds.append((amount, attributes))

        def record(self, amount, attributes=None):
            self.records.append((amount, attributes))

    totals, dur = FakeMetric(), FakeMetric()
    monkeypatch.setattr("tools.SEARCH_REQUESTS_TOTAL", totals)
    monkeypatch.setattr("tools.SEARCH_REQUEST_DURATION", dur)

    @_metrics_wrapper("serpapi_google")
    async def ok_fn():
        return {"status": "ok", "data": {}}

    @_metrics_wrapper("serpapi_google")
    async def err_fn():
        return {"status": "error", "message": "x"}

    await ok_fn()
    await err_fn()

    assert totals.adds[0][1] == {"provider": "serpapi",
                                 "engine": "serpapi_google", "status": "success"}
    assert totals.adds[1][1]["status"] == "error"
    assert len(dur.records) == 2
