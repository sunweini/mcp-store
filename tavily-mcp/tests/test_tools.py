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
    """Scriptable fake TavilyClient: records calls, fails per-endpoint.

    remaining_field: 非 None 时 search 响应自带 remaining（I-2 主路径
    验证用）；usage_calls 统计 usage() 调用次数（周期兜底验证用）。
    """

    def __init__(self, key, timeout=5.0, fail=None, fail_status=401,
                 remaining_field=None, fail_exc=None):
        self.key = key
        self.timeout = timeout
        self.fail = fail
        self.fail_status = fail_status
        self.remaining_field = remaining_field
        self.fail_exc = fail_exc  # 注入任意异常（模拟 httpx 超时/网络异常）
        self.calls = []
        self.closed = 0
        self.usage_calls = 0

    async def close(self):
        # 镜像真实 TavilyClient.close()（异步关闭 httpx.AsyncClient）。
        # 回归点：关闭方法名是 close 而非 aclose——写错则 _once 的
        # getattr 恒为 None、连接泄漏依旧且无测试能发现
        self.closed += 1

    async def usage(self):
        self.usage_calls += 1
        return {"plan_usage": {"search": {"remaining": 123}}}

    async def search(self, params):
        payload = {"results": [{"title": "t", "url": "u"}]}
        if self.remaining_field is not None:
            payload["remaining"] = self.remaining_field
        return await self._hit("search", params, payload)

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
        if self.fail_exc is not None:
            raise self.fail_exc
        if self.fail == endpoint:
            raise TavilyError(self.fail_status, "scripted failure")
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
                "k1": {"key_id": "k1", "key": "tvly-k1", "provider": "tavily",
                       "monthly_quota": 1000, "status": "active", "remaining": 900},
            }
        self._records = records
        self.errors = []
        self.successes = []
        self.releases = []
        self.success_remaining = []  # (key_id, remaining) 配对（I-2 断言用）

    async def next_key(self):
        for rec in self._records.values():
            if rec.get("status") not in ("invalid", "exhausted"):
                return rec
        return None

    async def on_success(self, key_id, remaining=None):
        self.successes.append(key_id)
        self.success_remaining.append((key_id, remaining))

    async def release(self, key_id):
        # 瞬时错误（不记账）路径的借用归还（Task 6 借用对称性）
        self.releases.append(key_id)

    async def on_error(self, key_id, kind, retry_after=None):
        self.errors.append((key_id, kind))
        if kind in (ErrorKind.INVALID, ErrorKind.EXHAUSTED):
            self._records[key_id]["status"] = kind.value


def _client_factory(made, fail=None, fail_status=401, fail_once=False,
                    remaining_field=None, fail_exc=None):
    """Build a client_factory; every created FakeClient appended to `made`.

    fail: endpoint 名（该 endpoint 所有调用失败）。fail_once: 仅第一个
    client 失败（failover 成功场景）；需与 fail 联用。
    remaining_field: 透传给 FakeClient（search 响应带 remaining 用）。
    fail_exc: 透传给 FakeClient（模拟 httpx 超时/网络异常用）。
    """
    def _make(key, timeout):
        client = FakeClient(key, timeout=timeout,
                            fail=(fail if not fail_once or not made else None),
                            fail_status=fail_status,
                            remaining_field=remaining_field,
                            fail_exc=fail_exc)
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
    assert made[0].closed == 1  # 每个 client 用完即关（N1：close 非 aclose）


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
    # on_success 未传 remaining，KeyPool 保留旧值 900——断言防回归
    # 「成功路径误改 remaining/配额」（评审 M3：断言意图在此）
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


async def test_rate_limit_backoff_before_retry(monkeypatch, fake_pool):
    """429 退避（spec C3）：幂等操作 429 重试前必须 sleep(0.5s) 起步，
    不立即重打冷却 key。断言 sleep 参数恰为 _RATE_LIMIT_BACKOFF——
    防退避丢失（曾只在代码注释里"规划"过）与参数漂移。

    用真实 KeyPool（fake_pool fixture）：RATE_LIMIT 置 cooldown 后
    next_key 排除 k1、自然轮到 k2（FakePool 不模拟 cooldown，会因
    key_id 相同被重试守卫挡住，测不出退避后的换 key）。"""
    import asyncio
    import tools.search as search_mod

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(search_mod.asyncio, "sleep", fake_sleep)
    made = []
    result = await tavily_search(
        "q", pool=fake_pool,
        client_factory=_client_factory(made, fail="search", fail_status=429,
                                       fail_once=True, remaining_field=555))
    assert result["status"] == "ok"  # k1 429 → 退避 → k2 成功
    assert [c.key for c in made] == ["tvly-a", "tvly-b"]
    assert slept == [search_mod._RATE_LIMIT_BACKOFF]  # 恰好退避一次
    assert fake_pool._records["k1"]["status"] == "cooldown"  # 冷却 key 不重打


async def test_non_rate_limit_no_backoff(monkeypatch, fake_pool):
    """非 429 失败（401 INVALID）换 key 重试不 sleep——退避只服务限流，
    失效 key 的 failover 应即时（退避会无谓拖慢合法 failover）。"""
    import tools.search as search_mod

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(search_mod.asyncio, "sleep", fake_sleep)
    made = []
    result = await tavily_search(
        "q", pool=fake_pool,
        client_factory=_client_factory(made, fail="search", fail_status=401,
                                       fail_once=True, remaining_field=555))
    assert result["status"] == "ok"
    assert [c.key for c in made] == ["tvly-a", "tvly-b"]
    assert slept == []  # 非 429 不退避


async def test_transient_error_releases_borrow():
    """瞬时错误（网络超时，classify_error 返回 None）不写 key 状态但必须
    归还借用（Task 6 借用对称性）——否则每次超时 in-flight +1 泄漏，
    key 被无限压低。FakePool.release 记录调用供断言。"""
    pool = FakePool(records={"k1": _rec("k1", "tvly-k1")})
    made = []
    result = await tavily_search("q", pool=pool,
                                 client_factory=_client_factory(made, fail_exc=_FakeTimeout()))
    assert result["status"] == "error"
    assert pool.errors == []            # 不记账（瞬时问题不写 key 状态）
    assert pool.releases == ["k1"]      # 但借用已归还


# ── 超时/网络错误不写 key 状态（实测 bug 回归）─────────────────────────────────


class _FakeTimeout(Exception):
    """模拟 httpx.ReadTimeout：无 status_code,classify_error 返回 None。

    真实场景：httpx.ReadTimeout/ConnectError 是瞬时问题,key 本身有效。
    曾因 kind or ErrorKind.EXHAUSTED 兜底把好 key 永久剔除（实测 serpapi
    一次 ReadTimeout 杀掉全部 key）——本组测试锁定「不写 key 状态」。
    """


async def test_timeout_does_not_mark_key_error():
    """单 key 超时 → status:error，key 状态不变（on_error 未被调用）。"""
    pool = FakePool(records={"k1": _rec("k1", "tvly-k1")})
    made = []
    result = await tavily_search("q", pool=pool,
                                 client_factory=_client_factory(made, fail_exc=_FakeTimeout()))
    assert result["status"] == "error"
    # 关键断言：on_error 一次都没被调用——超时不会剔除/冷却任何 key
    assert pool.errors == []
    assert pool._records["k1"]["status"] == "active"  # 下次请求仍可用
    assert len(made) == 1  # next_key 返回同一 key,守卫挡住不重试


async def test_timeout_second_key_failure_keeps_both_keys():
    """重试分支同样不写 key 状态：两 key 都超时 → error，但都保持 active。"""
    pool = FakePool(records={
        "k1": _rec("k1", "tvly-k1"),
        "k2": _rec("k2", "tvly-k2"),
    })
    made = []
    result = await tavily_search("q", pool=pool,
                                 client_factory=_client_factory(made, fail_exc=_FakeTimeout()))
    assert result["status"] == "error"
    assert pool.errors == []
    assert pool._records["k1"]["status"] == "active"
    assert pool._records["k2"]["status"] == "active"


async def test_retry_failover_timeout_on_second_key_keeps_second_key():
    """k1 401（可归类→标记 INVALID）→ 换 k2 重试,k2 超时（不可归类）→
    k2 不写状态。回归点：重试分支若仍兜底 EXHAUSTED,k2 会被永久剔除。"""
    pool = FakePool(records={
        "k1": _rec("k1", "tvly-k1"),
        "k2": _rec("k2", "tvly-k2"),
    })
    made = []

    def _make(key, timeout):
        client = FakeClient(key, timeout=timeout,
                            fail="search" if not made else None,
                            fail_exc=None if not made else _FakeTimeout())
        made.append(client)
        return client

    result = await tavily_search("q", pool=pool, client_factory=_make)
    assert result["status"] == "error"
    assert [c.key for c in made] == ["tvly-k1", "tvly-k2"]
    assert pool.errors == [("k1", ErrorKind.INVALID)]  # 只标记了 k1
    assert pool._records["k2"]["status"] == "active"  # k2 超时未被剔除


async def test_clients_closed_on_success_fail_and_failover():
    """每个创建的 client 都必须关闭（N1 回归：关闭方法名是 close 不是
    aclose——上轮错写 aclose 导致 getattr 恒为 None，连接泄漏无感知）。"""
    pool = FakePool(records={"k1": _rec("k1", "tvly-k1"), "k2": _rec("k2", "tvly-k2")})
    made = []

    # 成功路径
    await tavily_search("q", pool=FakePool(), client_factory=_client_factory(made))
    assert made[-1].closed == 1

    # 失败路径（无重试，crawl 不重试）
    pool2 = FakePool(records={"k1": _rec("k1", "tvly-k1")})
    made2 = []
    await tavily_crawl(["https://a"], pool=pool2,
                       client_factory=_client_factory(made2, fail="crawl"))
    assert made2[0].closed == 1

    # failover 路径（k1 失败 → k2 成功，两个 client 都要关）
    pool3 = FakePool(records={"k1": _rec("k1", "tvly-k1"), "k2": _rec("k2", "tvly-k2")})
    made3 = []
    await tavily_search("q", pool=pool3,
                        client_factory=_client_factory(made3, fail="search",
                                                       fail_once=True))
    assert [c.closed for c in made3] == [1, 1]


# ── remaining 刷新（final-review I-2）───────────────────────────────────────


async def test_response_remaining_written_to_pool():
    """主路径：search 响应自带 remaining → 直接回写 pool（零额外请求）。"""
    pool = FakePool()
    made = []
    result = await tavily_search("q", pool=pool,
                                 client_factory=_client_factory(made, remaining_field=777))
    assert result["status"] == "ok"
    assert pool.success_remaining == [("k1", 777)]
    assert made[0].usage_calls == 0  # 响应已有 remaining，不打 /usage


async def test_failover_writes_remaining_of_second_key():
    """failover 成功路径同样回写 remaining（k2 的 remaining 进 k2 记录）。"""
    pool = FakePool(records={
        "k1": _rec("k1", "tvly-k1"),
        "k2": _rec("k2", "tvly-k2"),
    })
    made = []
    result = await tavily_search(
        "q", pool=pool,
        client_factory=_client_factory(made, fail="search", fail_once=True,
                                       remaining_field=666))
    assert result["status"] == "ok"
    assert pool.success_remaining == [("k2", 666)]
    assert pool.successes == ["k2"]


async def test_usage_refresh_periodic_not_every_request(monkeypatch):
    """兜底路径：无 remaining 响应时周期调 usage（每 50 次成功 1 次），
    不是每次请求都打 /usage——防配额浪费（I-2 核心）。"""
    import tools.search as search_mod

    pool = FakePool()
    # 固定计数：第 50 次成功触发刷新（模 USAGE_REFRESH_INTERVAL）
    monkeypatch.setattr(search_mod, "_usage_refresh_counter", 49)
    made = []
    result = await tavily_search("q", pool=pool,
                                 client_factory=_client_factory(made))
    assert result["status"] == "ok"
    # usage 调用在新建的 usage client 上（搜索 client 已关）；made[1] 即它
    assert made[-1].usage_calls == 1  # 恰好在周期点 → 调了 usage
    assert pool.success_remaining == [("k1", 123)]  # FakeClient.usage 返回 123

    # 非周期点的请求不调 usage（下一次成功在计数 50→51）
    made2 = []
    pool2 = FakePool()
    await tavily_search("q", pool=pool2, client_factory=_client_factory(made2))
    assert made2[0].usage_calls == 0
    assert pool2.success_remaining == [("k1", None)]  # 无 remaining 可回写


async def test_usage_refresh_failure_is_silent():
    """usage 刷新失败不阻塞搜索：remaining 保持 None，结果仍 ok。"""
    pool = FakePool()
    made = []

    class FailUsageClient(FakeClient):
        async def usage(self):
            self.usage_calls += 1
            raise RuntimeError("usage endpoint down")

    import tools.search as search_mod
    search_mod._usage_refresh_counter = 49  # 触发周期点

    def _make(key, timeout):
        client = FailUsageClient(key, timeout=timeout)
        made.append(client)
        return client

    result = await tavily_search("q", pool=pool, client_factory=_make)
    assert result["status"] == "ok"
    assert made[-1].usage_calls == 1  # usage client 是新建的（made[-1]）
    assert pool.success_remaining == [("k1", None)]


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
