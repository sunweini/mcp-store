"""Tool layer tests — parameter passthrough, pool integration, errors.

工具是模块级函数(pool 显式传入),测试用 FakePool + FakeClient 驱动,
不涉及真实 HTTP 或 Redis。client_factory 注入点避免依赖私有 _get。
"""
from key_pool import ErrorKind
from brave_client import BraveError
from tools.web import brave_web_search, brave_local_search


class FakeClient:
    """Scriptable fake BraveClient: records calls, fails per-endpoint."""

    def __init__(self, key, timeout=5.0, fail=None, fail_status=401):
        self.key = key
        self.timeout = timeout
        self.fail = fail
        self.fail_status = fail_status
        self.calls = []
        self.closed = 0

    async def close(self):
        # 镜像真实 BraveClient.close()（异步关闭 httpx.AsyncClient）。
        # 回归点：关闭方法名是 close 而非 aclose——写错则 _once 的
        # getattr 恒为 None、连接泄漏依旧且无测试能发现
        self.closed += 1

    async def web_search(self, params):
        return await self._hit("web_search", params, {"web": {"results": [{"title": "t", "url": "u"}]}})

    async def local_search(self, params):
        return await self._hit("local_search", params, {"local": {"results": [{"title": "t"}]}})

    async def _hit(self, endpoint, params, ok_payload):
        self.calls.append((endpoint, dict(params)))
        if self.fail == endpoint:
            raise BraveError(self.fail_status, "scripted failure")
        return ok_payload


class FakePool:
    """In-memory KeyPool stand-in.

    next_key 跳过 invalid/exhausted;on_error 按 kind 标记 key,
    使 failover 场景 next_key 自然轮换到下一个可用 key。
    _records 仅作数据存储（I1 后工具层不再依赖它判空，重试可行性
    完全由 next_key 语义承载）。
    """

    def __init__(self, records=None):
        if records is None:
            records = {
                "k1": {"key_id": "k1", "key": "BSA-k1", "provider": "brave",
                       "monthly_quota": 2000, "status": "active", "remaining": 1900},
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


def _client_factory(made, fail=None, fail_status=401, fail_once=False):
    """Build a client_factory; every created FakeClient appended to `made`.

    fail: endpoint 名（该 endpoint 所有调用失败）。fail_once: 仅第一个
    client 失败（failover 成功场景）；需与 fail 联用。
    """
    def _make(key, timeout):
        client = FakeClient(key, timeout=timeout,
                            fail=(fail if not fail_once or not made else None),
                            fail_status=fail_status)
        made.append(client)
        return client
    return _make


def _rec(key_id, key, **over):
    rec = {"key_id": key_id, "key": key, "provider": "brave",
           "monthly_quota": 2000, "status": "active", "remaining": 1900}
    rec.update(over)
    return rec


# ── brief 指定用例 ──────────────────────────────────────────────────────────


async def test_brave_web_search_passes_params():
    pool = FakePool()
    made = []
    result = await brave_web_search(
        "hello world", count=3, pool=pool, client_factory=_client_factory(made))
    assert result["status"] == "ok"
    assert made[0].calls == [("web_search", {"q": "hello world", "count": 3, "offset": 0})]
    assert made[0].timeout == 5.0
    assert pool.successes == ["k1"]
    assert made[0].closed == 1  # 每个 client 用完即关（N1：close 非 aclose）


async def test_brave_web_search_invalid_params_returns_error():
    result = await brave_web_search("", pool=FakePool())
    assert result["status"] == "error"
    result = await brave_web_search("q", count=0, pool=FakePool())
    assert result["status"] == "error"
    result = await brave_web_search("q", count=21, pool=FakePool())
    assert result["status"] == "error"
    result = await brave_web_search("q", offset=-1, pool=FakePool())
    assert result["status"] == "error"
    result = await brave_web_search("q", offset=10, pool=FakePool())
    assert result["status"] == "error"


async def test_brave_web_search_no_keys_returns_error():
    pool = FakePool(records={})
    result = await brave_web_search("q", pool=pool)
    assert result["status"] == "error"
    assert "不可用" in result["message"]
    assert not pool.successes


# ── 参数透传 / 边界 ─────────────────────────────────────────────────────────


async def test_brave_web_search_with_fixture_pool(fake_pool):
    """真实 KeyPool（FakeRedis 驱动）集成：成功记账写回 Redis。"""
    made = []
    result = await brave_web_search("q", pool=fake_pool, client_factory=_client_factory(made))
    assert result["status"] == "ok"
    assert made[0].key == "BSA-a"  # next_key 选剩余最高的 k1
    # on_success 未传 remaining，KeyPool 保留旧值 1900——断言防回归
    # 「成功路径误改 remaining/配额」（评审 M3：断言意图在此）
    assert fake_pool._records["k1"]["remaining"] == 1900
    assert fake_pool._records["k1"]["last_used_at"] is not None
    assert fake_pool._records["k1"]["last_error"] is None


async def test_brave_local_search_passes_params():
    made = []
    result = await brave_local_search(
        "pizza", count=8, pool=FakePool(), client_factory=_client_factory(made))
    assert result["status"] == "ok"
    assert made[0].calls == [("local_search", {"q": "pizza", "count": 8})]


async def test_brave_local_search_invalid_params_returns_error():
    result = await brave_local_search("", pool=FakePool())
    assert result["status"] == "error"
    result = await brave_local_search("q", count=21, pool=FakePool())
    assert result["status"] == "error"
    result = await brave_local_search("q", count=0, pool=FakePool())
    assert result["status"] == "error"


async def test_brave_web_search_passes_offset():
    made = []
    await brave_web_search("q", offset=5, pool=FakePool(),
                           client_factory=_client_factory(made))
    assert made[0].calls[0][1]["offset"] == 5


# ── failover / 重试策略 ─────────────────────────────────────────────────────


async def test_brave_web_search_failover_retries_next_key():
    """k1 401 → 剔除 → 重试 k2 成功（幂等查询才允许的 failover）。"""
    pool = FakePool(records={
        "k1": _rec("k1", "BSA-k1"),
        "k2": _rec("k2", "BSA-k2"),
    })
    made = []
    result = await brave_web_search(
        "q", pool=pool,
        client_factory=_client_factory(made, fail="web_search", fail_once=True))
    assert result["status"] == "ok"
    assert [c.key for c in made] == ["BSA-k1", "BSA-k2"]  # 换 key 重试
    assert pool.errors == [("k1", ErrorKind.INVALID)]
    assert pool.successes == ["k2"]


async def test_failover_second_key_failure_returns_error():
    pool = FakePool(records={"k1": _rec("k1", "BSA-k1"), "k2": _rec("k2", "BSA-k2")})
    made = []
    result = await brave_web_search("q", pool=pool,
                                    client_factory=_client_factory(made, fail="web_search"))
    # 两个 key 都 401 → error,且两个 key 都被标记剔除
    assert result["status"] == "error"
    assert pool.errors == [("k1", ErrorKind.INVALID), ("k2", ErrorKind.INVALID)]
    assert made[0].key == "BSA-k1" and made[1].key == "BSA-k2"


async def test_rate_limit_does_not_retry_same_key():
    pool = FakePool(records={"k1": _rec("k1", "BSA-k1")})
    made = []
    # 429 → RATE_LIMIT;重试要求不同 key_id,单 key 池不重试
    result = await brave_web_search("q", pool=pool,
                                    client_factory=_client_factory(made, fail="web_search",
                                                                   fail_status=429))
    assert result["status"] == "error"
    assert pool.errors == [("k1", ErrorKind.RATE_LIMIT)]
    assert len(made) == 1


async def test_local_search_failover_retries_next_key():
    """local_search 同样是幂等 GET,失败换 key 重试一次。"""
    pool = FakePool(records={"k1": _rec("k1", "BSA-k1"), "k2": _rec("k2", "BSA-k2")})
    made = []
    result = await brave_local_search(
        "q", pool=pool,
        client_factory=_client_factory(made, fail="local_search", fail_once=True))
    assert result["status"] == "ok"
    assert [c.key for c in made] == ["BSA-k1", "BSA-k2"]


async def test_clients_closed_on_success_fail_and_failover():
    """每个创建的 client 都必须关闭（N1 回归：关闭方法名是 close 不是
    aclose——tavily 上轮错写 aclose 导致 getattr 恒为 None，连接泄漏无感知）。"""
    # 成功路径
    made = []
    await brave_web_search("q", pool=FakePool(), client_factory=_client_factory(made))
    assert made[-1].closed == 1

    # 失败路径（k1 401 后无第二 key → 不再重试）
    pool2 = FakePool(records={"k1": _rec("k1", "BSA-k1")})
    made2 = []
    await brave_web_search("q", pool=pool2,
                           client_factory=_client_factory(made2, fail="web_search"))
    assert made2[0].closed == 1

    # failover 路径（k1 失败 → k2 成功，两个 client 都要关）
    pool3 = FakePool(records={"k1": _rec("k1", "BSA-k1"), "k2": _rec("k2", "BSA-k2")})
    made3 = []
    await brave_web_search("q", pool=pool3,
                           client_factory=_client_factory(made3, fail="web_search",
                                                          fail_once=True))
    assert [c.closed for c in made3] == [1, 1]


# ── MCP 注册 ────────────────────────────────────────────────────────────────


async def test_register_exposes_two_readonly_tools():
    from fastmcp import FastMCP
    from tools.web import register
    mcp = FastMCP("test")
    register(mcp, lambda: FakePool())
    tools = await mcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == ["brave_local_search", "brave_web_search"]
    for t in tools:
        assert t.annotations.read_only_hint is True
        assert t.description  # docstring 生成 description


async def test_register_schema_exposes_params_not_pool():
    from fastmcp import FastMCP
    from tools.web import register
    mcp = FastMCP("test")
    register(mcp, lambda: FakePool())
    tools = {t.name: t for t in await mcp.list_tools()}
    web_props = (tools["brave_web_search"].parameters or {}).get("properties", {})
    for p in ("query", "count", "offset"):
        assert p in web_props
    assert "pool" not in web_props  # 注入参数不出现在 schema
    local_props = (tools["brave_local_search"].parameters or {}).get("properties", {})
    for p in ("query", "count"):
        assert p in local_props
    assert "pool" not in local_props


async def test_register_tools_aggregator():
    from fastmcp import FastMCP
    from tools import register_tools
    mcp = FastMCP("test")
    register_tools(mcp, lambda: FakePool())
    names = {t.name for t in await mcp.list_tools()}
    assert names == {"brave_web_search", "brave_local_search"}


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

    @_metrics_wrapper("brave_web_search")
    async def ok_fn():
        return {"status": "ok", "data": {}}

    @_metrics_wrapper("brave_web_search")
    async def err_fn():
        return {"status": "error", "message": "x"}

    await ok_fn()
    await err_fn()

    assert totals.adds[0][1] == {"provider": "brave",
                                 "engine": "brave_web_search", "status": "success"}
    assert totals.adds[1][1]["status"] == "error"
    assert len(dur.records) == 2
