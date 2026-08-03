"""Tool layer tests — parameter passthrough, pool integration, errors.

工具是模块级函数(pool 显式传入),测试用 FakePool + FakeClient 驱动,
不涉及真实 HTTP 或 Redis。client_factory 注入点避免依赖私有 _post。
"""
from key_pool import ErrorKind
from tavily_client import TavilyError
from tools.search import (
    tavily_search, tavily_extract, tavily_crawl, tavily_map, tavily_research,
)


class FakeClient:
    """Scriptable fake TavilyClient: records calls, fails per-endpoint."""

    def __init__(self, key, timeout=5.0, fail=None, fail_status=401):
        self.key = key
        self.timeout = timeout
        self.fail = fail
        self.fail_status = fail_status
        self.calls = []

    async def search(self, params):
        return await self._hit("search", params, {"results": [{"title": "t", "url": "u"}]})

    async def extract(self, params):
        return await self._hit("extract", params, {"results": [{"url": "u", "raw_content": "x"}]})

    async def crawl(self, params):
        return await self._hit("crawl", params, {"results": [{"url": "u", "content": "x"}]})

    async def map(self, params):
        return await self._hit("map", params, {"urls": ["https://a", "https://b"]})

    async def research(self, params):
        return await self._hit("research", params, {"response": "answer", "sources": []})

    async def _hit(self, endpoint, params, ok_payload):
        self.calls.append((endpoint, dict(params)))
        if self.fail == endpoint:
            raise TavilyError(self.fail_status, "scripted failure")
        return ok_payload


class FakePool:
    """In-memory KeyPool stand-in.

    next_key 跳过 invalid/exhausted;on_error 按 kind 标记 key,
    使 failover 场景 next_key 自然轮换到下一个可用 key。
    _records 属性名与 KeyPool 一致(工具层重试判空依赖)。
    """

    def __init__(self, records=None):
        if records is None:
            records = {
                "k1": {"key_id": "k1", "key": "tvly-k1", "provider": "tavily",
                       "monthly_quota": 1000, "status": "active", "remaining": 900},
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
    rec = {"key_id": key_id, "key": key, "provider": "tavily",
           "monthly_quota": 1000, "status": "active", "remaining": 900}
    rec.update(over)
    return rec


# ── brief 指定用例 ──────────────────────────────────────────────────────────


async def test_tavily_search_passes_params():
    pool = FakePool()
    made = []
    result = await tavily_search(
        "hello world", max_results=3, pool=pool, client_factory=_client_factory(made))
    assert result["status"] == "ok"
    assert made[0].calls == [("search", {
        "query": "hello world", "search_depth": "basic", "topic": "general",
        "max_results": 3, "include_answer": False,
        "include_raw_content": False, "include_images": False,
    })]
    assert made[0].timeout == 5.0
    assert pool.successes == ["k1"]


async def test_tavily_search_invalid_params_returns_error():
    result = await tavily_search("", pool=FakePool())
    assert result["status"] == "error"


async def test_tavily_search_no_keys_returns_error():
    pool = FakePool(records={})
    result = await tavily_search("q", pool=pool)
    assert result["status"] == "error"
    assert "不可用" in result["message"]
    assert not pool.successes


# ── 参数透传 / 边界 ─────────────────────────────────────────────────────────


async def test_tavily_search_with_fixture_pool(fake_pool):
    """真实 KeyPool（FakeRedis 驱动）集成：成功记账写回 Redis。"""
    made = []
    result = await tavily_search("q", pool=fake_pool, client_factory=_client_factory(made))
    assert result["status"] == "ok"
    assert made[0].key == "tvly-a"  # next_key 选剩余最高的 k1
    # on_success 持久化到 Redis hash（k1 的 remaining 更新）
    assert fake_pool._records["k1"]["remaining"] == 900
    assert fake_pool._records["k1"]["last_used_at"] is not None
    assert fake_pool._records["k1"]["last_error"] is None


async def test_tavily_extract_passes_params():
    made = []
    result = await tavily_extract(
        ["https://a", "https://b"], extract_depth="advanced",
        pool=FakePool(), client_factory=_client_factory(made))
    assert result["status"] == "ok"
    assert made[0].calls == [("extract", {
        "urls": ["https://a", "https://b"], "extract_depth": "advanced"})]


async def test_tavily_map_passes_params():
    made = []
    result = await tavily_map("q", max_results=50, pool=FakePool(),
                              client_factory=_client_factory(made))
    assert result["status"] == "ok"
    assert made[0].calls == [("map", {"query": "q", "search_depth": "basic",
                                      "max_results": 50})]


async def test_tavily_crawl_passes_params_and_long_timeout():
    made = []
    result = await tavily_crawl(["https://a"], max_depth=2, pool=FakePool(),
                                client_factory=_client_factory(made))
    assert result["status"] == "ok"
    assert made[0].calls == [("crawl", {"urls": ["https://a"], "max_depth": 2,
                                        "max_pages": 20, "max_cost": 10.0})]
    assert made[0].timeout == 60.0  # 长任务专用超时


async def test_tavily_research_passes_params_and_long_timeout():
    made = []
    result = await tavily_research("q", max_learnings=3, pool=FakePool(),
                                   client_factory=_client_factory(made))
    assert result["status"] == "ok"
    assert made[0].calls == [("research", {"query": "q", "max_depth": 3,
                                           "max_learnings": 3, "max_sources": 5,
                                           "max_browser_pages": 20})]
    assert made[0].timeout == 60.0


async def test_tavily_search_clamps_max_results():
    made = []
    await tavily_search("q", max_results=999, pool=FakePool(),
                        client_factory=_client_factory(made))
    assert made[0].calls[0][1]["max_results"] == 20


async def test_tavily_map_clamps_max_results():
    made = []
    await tavily_map("q", max_results=999, pool=FakePool(),
                     client_factory=_client_factory(made))
    assert made[0].calls[0][1]["max_results"] == 100


async def test_tavily_crawl_limits_urls_to_5():
    made = []
    await tavily_crawl([f"https://x{i}" for i in range(10)], pool=FakePool(),
                       client_factory=_client_factory(made))
    assert len(made[0].calls[0][1]["urls"]) == 5


async def test_tavily_extract_limits_urls_to_10():
    made = []
    await tavily_extract([f"https://x{i}" for i in range(20)], pool=FakePool(),
                         client_factory=_client_factory(made))
    assert len(made[0].calls[0][1]["urls"]) == 10


async def test_tavily_extract_empty_urls_error():
    result = await tavily_extract([], pool=FakePool())
    assert result["status"] == "error"


# ── failover / 重试策略 ─────────────────────────────────────────────────────


async def test_tavily_search_failover_retries_next_key():
    """k1 401 → 剔除 → 重试 k2 成功（幂等查询才允许的 failover）。"""
    pool = FakePool(records={
        "k1": _rec("k1", "tvly-k1"),
        "k2": _rec("k2", "tvly-k2"),
    })
    made = []
    result = await tavily_search(
        "q", pool=pool,
        client_factory=_client_factory(made, fail="search", fail_once=True))
    assert result["status"] == "ok"
    assert [c.key for c in made] == ["tvly-k1", "tvly-k2"]  # 换 key 重试
    assert pool.errors == [("k1", ErrorKind.INVALID)]
    assert pool.successes == ["k2"]


async def test_tavily_research_no_retry():
    pool = FakePool(records={
        "k1": _rec("k1", "tvly-k1"),
        "k2": _rec("k2", "tvly-k2"),
    })
    made = []
    result = await tavily_research("q", pool=pool,
                                   client_factory=_client_factory(made, fail="research"))
    assert result["status"] == "error"
    assert len(made) == 1  # 长任务不重试,只试了一个 key
    assert pool.errors == [("k1", ErrorKind.INVALID)]
    assert not pool.successes


async def test_tavily_crawl_no_retry():
    pool = FakePool(records={"k1": _rec("k1", "tvly-k1"), "k2": _rec("k2", "tvly-k2")})
    made = []
    result = await tavily_crawl(["https://a"], pool=pool,
                                client_factory=_client_factory(made, fail="crawl"))
    assert result["status"] == "error"
    assert len(made) == 1


async def test_failover_second_key_failure_returns_error():
    pool = FakePool(records={"k1": _rec("k1", "tvly-k1"), "k2": _rec("k2", "tvly-k2")})
    made = []
    result = await tavily_search("q", pool=pool,
                                 client_factory=_client_factory(made, fail="search"))
    # 两个 key 都 401 → error,且两个 key 都被标记剔除
    assert result["status"] == "error"
    assert pool.errors == [("k1", ErrorKind.INVALID), ("k2", ErrorKind.INVALID)]
    assert made[0].key == "tvly-k1" and made[1].key == "tvly-k2"


async def test_rate_limit_does_not_retry_same_key():
    pool = FakePool(records={"k1": _rec("k1", "tvly-k1")})
    made = []
    # 429 → RATE_LIMIT;重试要求不同 key_id,单 key 池不重试
    result = await tavily_search("q", pool=pool,
                                 client_factory=_client_factory(made, fail="search",
                                                                fail_status=429))
    assert result["status"] == "error"
    assert pool.errors == [("k1", ErrorKind.RATE_LIMIT)]
    assert len(made) == 1


# ── MCP 注册 ────────────────────────────────────────────────────────────────


async def test_register_exposes_five_readonly_tools():
    from fastmcp import FastMCP
    from tools.search import register
    mcp = FastMCP("test")
    register(mcp, lambda: FakePool())
    tools = await mcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == ["tavily_crawl", "tavily_extract", "tavily_map",
                     "tavily_research", "tavily_search"]
    for t in tools:
        assert t.annotations.read_only_hint is True
        assert t.description  # docstring 生成 description


async def test_register_schema_exposes_params_not_pool():
    from fastmcp import FastMCP
    from tools.search import register
    mcp = FastMCP("test")
    register(mcp, lambda: FakePool())
    tools = {t.name: t for t in await mcp.list_tools()}
    search_props = (tools["tavily_search"].parameters or {}).get("properties", {})
    for p in ("query", "search_depth", "topic", "days", "max_results",
              "include_answer", "include_raw_content", "include_images"):
        assert p in search_props
    assert "pool" not in search_props  # 注入参数不出现在 schema


async def test_register_tools_aggregator():
    from fastmcp import FastMCP
    from tools import register_tools
    mcp = FastMCP("test")
    register_tools(mcp, lambda: FakePool())
    names = {t.name for t in await mcp.list_tools()}
    assert names == {"tavily_search", "tavily_extract", "tavily_crawl",
                     "tavily_map", "tavily_research"}


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

    @_metrics_wrapper("tavily_search")
    async def ok_fn():
        return {"status": "ok", "data": {}}

    @_metrics_wrapper("tavily_search")
    async def err_fn():
        return {"status": "error", "message": "x"}

    await ok_fn()
    await err_fn()

    assert totals.adds[0][1] == {"provider": "tavily",
                                 "engine": "tavily_search", "status": "success"}
    assert totals.adds[1][1]["status"] == "error"
    assert len(dur.records) == 2
