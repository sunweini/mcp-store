"""Tavily search tools.

Design note: functions at module level (not closures) so tests import
and call them directly with a FakePool. register() creates thin MCP
wrappers injecting the real pool.

Error/failover strategy (per spec 错误处理节):
- 幂等轻查询 (search/extract/map): 失败后换下一 key 重试一次
- 长任务 (crawl/research): 不重试 — 重跑成本高,失败留给用户决定
- client_factory 注入: 默认构造真实 TavilyClient;crawl/research 用 60s
  超时(长任务),其余 5s。测试注入 FakeClient 驱动公开方法,不依赖私有 _post。

Concurrency (spec C2/C3, Task 6):
- 借用语义：next_key 扣减 in-flight 防并发扎堆同一 key（429 风暴）；
  成功/可归类失败由 on_success/on_error 归还，瞬时错误（不记账路径）
  显式 release() 归还——借用必须对称，否则超时每次泄漏 +1
- 429 退避：幂等操作 429 时 sleep(0.5s) 再换 key（冷却 key 已被
  next_key 排除，不立即重打）
- per-endpoint semaphore：长任务小并发（crawl/research ≤5），防并发
  打爆外部 API；轻查询 ≤20
"""
import asyncio
from typing import Callable, Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
import structlog

from key_pool import ErrorKind
from tavily_client import TavilyClient, classify_error

logger = structlog.get_logger()

# 长任务超时：research 一轮 gather 可达分钟级,5s 默认超时会误杀
LONG_TASK_TIMEOUT = 60.0
DEFAULT_TIMEOUT = 5.0

# remaining 刷新策略（final-review I-2）：
# - 主路径：search 响应自带 remaining 字段则直接回写——零额外 API 请求
# - 兜底：每 USAGE_REFRESH_INTERVAL 次成功请求调一次 GET /usage 刷新
#   官方 remaining（50:1 的请求比，防止每次成功都打 /usage 浪费配额；
#   也避免长期不刷新导致 low_quota 轮换基于陈旧数据）
# 放在工具层而非 KeyPool：usage() 属于 TavilyClient，KeyPool 保持
# provider 无关（brave/serpapi 无官方 remaining 端点，不背此逻辑）
USAGE_REFRESH_INTERVAL = 50
_usage_refresh_counter = 0

# 单一来源：工具名 → endpoint/超时/重试策略。endpoint 即 TavilyClient
# 公开方法名（search/extract/crawl/map/research）；超时区分长任务（60s）
# 与轻查询（5s）；重试仅幂等操作允许（spec 错误处理节）。曾有三套平行
# 映射（RETRYABLE/NO_RETRY 按工具名 + endpoint 名），改动需同步多处，
# 现收敛为一张表——新增/调整工具的唯一切入点（I3）。
TOOLS: dict[str, dict] = {
    "tavily_search": {"endpoint": "search", "timeout": DEFAULT_TIMEOUT, "retryable": True},
    "tavily_extract": {"endpoint": "extract", "timeout": DEFAULT_TIMEOUT, "retryable": True},
    "tavily_map": {"endpoint": "map", "timeout": DEFAULT_TIMEOUT, "retryable": True},
    "tavily_crawl": {"endpoint": "crawl", "timeout": LONG_TASK_TIMEOUT, "retryable": False},
    "tavily_research": {"endpoint": "research", "timeout": LONG_TASK_TIMEOUT, "retryable": False},
}

# client_factory(key, timeout) -> TavilyClient 兼容对象(公开方法 search/
# extract/crawl/map/research)。默认造真实 client;测试注入 FakeClient。
ClientFactory = Callable[[str, float], object]

# ── per-endpoint 并发上限（spec C2，Task 6）────────────────────────
# 长任务（crawl/research）并发会长时间占住外部 API 连接，小并发上限防
# 打爆外部 API；轻查询放宽到 20（幂等 + 快，主要受 next_key 借用分散保护）。
# 模块级 dict 惰性建 semaphore（工具层单例，测试不触真实并发路径）。
_RATE_LIMIT_BACKOFF = 0.5   # 429 重试退避起点（秒）
_TOOL_CONCURRENCY = {
    "tavily_search": 20,
    "tavily_extract": 20,
    "tavily_map": 20,
    "tavily_crawl": 5,
    "tavily_research": 5,
}
_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_semaphore(tool_name: str) -> asyncio.Semaphore:
    sem = _semaphores.get(tool_name)
    if sem is None:
        sem = asyncio.Semaphore(_TOOL_CONCURRENCY[tool_name])
        _semaphores[tool_name] = sem
    return sem


def _default_factory(key: str, timeout: float) -> TavilyClient:
    return TavilyClient(key, timeout=timeout)


async def _call_with_pool(pool, tool_name: str, params: dict,
                          client_factory: Optional[ClientFactory] = None) -> dict:
    """Pick key → call API → report result to pool. One retry on failover.

    Returns the tool response dict (status ok/error).
    client_factory: (key, timeout) -> client。默认真实 TavilyClient;
    测试注入 FakeClient。timeout/endpoint/重试策略均取自 TOOLS 表。

    重试可行性由 next_key() 语义承载（不触碰 pool 私有属性——I1）：
    失败 key 已被 on_error 标记（invalid/exhausted 永久跳过、cooldown
    冷却中），next_key 返回 None 或同一 key 即表示没有可换的 key。
    """
    cfg = TOOLS[tool_name]
    endpoint = cfg["endpoint"]
    timeout = cfg["timeout"]
    retryable = cfg["retryable"]
    factory = client_factory or _default_factory
    sem = _get_semaphore(tool_name)

    async def _once(rec: dict) -> tuple:
        """Single attempt: returns (resp, remaining_or_None, exc).

        remaining 从响应体顶层 remaining 字段取（Tavily 官方 remaining
        会随 search 响应返回时用它——零额外请求；响应无该字段时为
        None，交给 _maybe_refresh_usage 周期兜底）。FakeClient 无
        remaining 时 getattr 兜底为 None。

        finally 里 close：真实 TavilyClient 共享进程级 httpx 连接池
        （tavily_client.get_shared_client，C1），close() 幂等——共享
        client 进程级存活不关闭（close 内部判断非共享才 aclose），
        测试注入的自建 client 由 close 归还有关权。FakeClient 无
        close 时 getattr 兜底跳过。
        """
        client = factory(rec["key"], timeout)
        try:
            # endpoint 来自模块常量（非用户输入），getattr 安全；
            # TavilyClient 方法名与 endpoint 同名
            resp = await getattr(client, endpoint)(params)
            remaining = resp.get("remaining") if isinstance(resp, dict) else None
            return resp, remaining, None
        except Exception as exc:
            return None, None, exc
        finally:
            closer = getattr(client, "close", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass

    async def _report_success(rec: dict, remaining) -> None:
        """成功记账 + remaining 刷新（I-2）。

        响应自带 remaining 优先（新鲜、零成本，见 _once）；缺失时走
        周期 usage() 兜底（_usage_refresh_interval_for 控制请求比）。
        记账统一在此执行，刷新只回传 remaining——避免双写。

        注意（Task 6 借用语义）：_maybe_refresh_usage 的 GET /usage 外呼
        发生在 on_success 归还借用之前——usage 刷新在借出窗口内属预期
        （最坏 5s 间隔一次、延迟 in-flight 占位，无正确性影响；若把归还
        提前到刷新前则需在 KeyPool 加「借回」，复杂度不值）。
        """
        if remaining is None:
            remaining = await _maybe_refresh_usage(
                pool, rec, factory, _usage_refresh_interval_for())
        await pool.on_success(rec["key_id"], remaining=remaining)

    # semaphore 防并发打爆外部 API（spec C2）：acquire 在取 key 之前——
    # 超过并发上限的请求排队等 semaphore 而非挤占 key 池借用
    async with sem:
        key_rec = await pool.next_key()
        if key_rec is None:
            return {"status": "error",
                    "message": "tavily 该源所有 API key 不可用，请在前台检查 key 池状态"}
        resp, remaining, exc = await _once(key_rec)
        if resp is not None:
            await _report_success(key_rec, remaining)
            return {"status": "ok", "data": resp}
        kind = classify_error(exc, getattr(exc, "status_code", None))
        # 仅可归类错误才写 key 状态（实测超时/连接错误 classify_error 返回
        # None——瞬时问题,key 本身有效,写 EXHAUSTED 会把好 key 永久剔除,
        # 曾致 serpapi 一次 ReadTimeout 杀掉全部 key）
        if kind:
            await pool.on_error(key_rec["key_id"], kind)
        else:
            # 瞬时错误不记账但必须归还借用——否则每次超时 in-flight +1
            # 永不清零，该 key 被无限压低（借用对称性，Task 6）
            await pool.release(key_rec["key_id"])
        if retryable:
            # 429/冷却：指数退避再重试（0.5s 起步，spec C3），不立即重打
            # 冷却 key——next_key 已排除 cooldown key，刚标记的 key 不会
            # 被选回；退避给外部 API 恢复窗口
            if kind == ErrorKind.RATE_LIMIT:
                await asyncio.sleep(_RATE_LIMIT_BACKOFF)
            # 换下一 key 重试一次（幂等操作才允许）；next_key 已排除刚标记
            # 失败的 key，None 或同 key 均表示无可换 key
            key_rec2 = await pool.next_key()
            if key_rec2 and key_rec2["key_id"] != key_rec["key_id"]:
                resp2, remaining2, exc2 = await _once(key_rec2)
                if resp2 is not None:
                    await _report_success(key_rec2, remaining2)
                    return {"status": "ok", "data": resp2}
                kind2 = classify_error(exc2, getattr(exc2, "status_code", None))
                if kind2:
                    await pool.on_error(key_rec2["key_id"], kind2)
                else:
                    await pool.release(key_rec2["key_id"])
        return {"status": "error", "message": str(exc)}


async def _maybe_refresh_usage(pool, rec: dict, factory: ClientFactory,
                               refresh_now: bool) -> int | None:
    """周期兜底：每 USAGE_REFRESH_INTERVAL 次成功请求调一次 /usage。

    只在 refresh_now 时打 /usage（全局计数已到周期点），否则零请求。
    只返回官方 remaining，不在此记账——on_success 由调用方统一执行，
    避免同一次成功被双写（usage 计数 zset 双倍、Redis 双写）。
    异常静默（usage 刷新失败不阻塞搜索——low_quota 保护只是增强，
    数据陈旧一点不致命）；返回 None 表示未取到。
    """
    if not refresh_now:
        return None
    client = factory(rec["key"], timeout=DEFAULT_TIMEOUT)
    try:
        body = await client.usage()
        usage = body.get("plan_usage", {}) if isinstance(body, dict) else {}
        search_usage = usage.get("search", {}) if isinstance(usage, dict) else {}
        return search_usage.get("remaining")
    except Exception:
        # 刷新失败不致命：保留现有 remaining，下次周期再试
        return None
    finally:
        closer = getattr(client, "close", None)
        if closer is not None:
            try:
                await closer()
            except Exception:
                pass


def _usage_refresh_interval_for() -> bool:
    """当前请求是否轮到 usage 周期刷新（进程级计数，成功请求递增）。

    counter 在「未轮到」时也递增：用 1/N 判定而非取模后置零——
    避免连续 N 个成功请求在同一秒内全部触发（无意义地扎堆打 /usage）。
    """
    global _usage_refresh_counter
    _usage_refresh_counter += 1
    return _usage_refresh_counter % USAGE_REFRESH_INTERVAL == 0


async def tavily_search(
    query: str,
    search_depth: str = "basic",
    topic: str = "general",
    days: int | None = None,
    max_results: int = 5,
    include_answer: bool = False,
    include_raw_content: bool = False,
    include_images: bool = False,
    *,
    pool,
    client_factory: Optional[ClientFactory] = None,
) -> dict:
    """Web search via Tavily. Returns organic results with title/url/content.

    query: 搜索词。search_depth: basic/advanced。topic: general/news/finance。
    max_results: 1-20。include_answer: 附带 AI 摘要答案。
    """
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    params = {
        "query": query,
        "search_depth": search_depth,
        "topic": topic,
        "max_results": min(max_results, 20),
        "include_answer": include_answer,
        "include_raw_content": include_raw_content,
        "include_images": include_images,
    }
    if days is not None:
        params["days"] = days
    return await _call_with_pool(pool, "tavily_search", params,
                                 client_factory=client_factory)


async def tavily_extract(urls: list[str], extract_depth: str = "basic", *, pool,
                         client_factory: Optional[ClientFactory] = None) -> dict:
    """Extract clean text content from URLs. urls: 1-10 个 URL 列表。"""
    if not urls:
        return {"status": "error", "message": "urls 不能为空"}
    params = {"urls": urls[:10], "extract_depth": extract_depth}
    return await _call_with_pool(pool, "tavily_extract", params,
                                 client_factory=client_factory)


async def tavily_crawl(urls: list[str], max_depth: int = 3, max_pages: int = 20,
                       max_cost: float = 10.0, *, pool,
                       client_factory: Optional[ClientFactory] = None) -> dict:
    """Crawl websites, return structured data. 长任务 — 不自动重试。"""
    if not urls:
        return {"status": "error", "message": "urls 不能为空"}
    params = {"urls": urls[:5], "max_depth": max_depth,
              "max_pages": max_pages, "max_cost": max_cost}
    return await _call_with_pool(pool, "tavily_crawl", params,
                                 client_factory=client_factory)


async def tavily_map(query: str, search_depth: str = "basic", max_results: int = 100,
                     *, pool, client_factory: Optional[ClientFactory] = None) -> dict:
    """Map search — return URLs across many topics for a query."""
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    params = {"query": query, "search_depth": search_depth,
              "max_results": min(max_results, 100)}
    return await _call_with_pool(pool, "tavily_map", params,
                                 client_factory=client_factory)


async def tavily_research(query: str, max_depth: int = 3, max_learnings: int = 5,
                          max_sources: int = 5, max_browser_pages: int = 20,
                          *, pool, client_factory: Optional[ClientFactory] = None) -> dict:
    """Deep research — gather info from multiple sources, return answer. 长任务不重试。"""
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    params = {"query": query, "max_depth": max_depth, "max_learnings": max_learnings,
              "max_sources": max_sources, "max_browser_pages": max_browser_pages}
    return await _call_with_pool(pool, "tavily_research", params,
                                 client_factory=client_factory)


def register(mcp: FastMCP, get_pool, metrics=None) -> None:
    """Register all tavily tools. get_pool: callable returning the KeyPool.

    Note: 每个工具用显式具名包装而非 *args 泛型包装——FastMCP v4
    不支持 *args 工具函数（ParsedFunction 校验拒绝），参数必须显式
    声明才能生成正确 inputSchema。pool/client_factory 是注入参数，
    不暴露给 MCP client（不出现在 schema）。

    description 的单一来源是 mcp.tool(description=...) 的显式参数，
    不依赖包装函数 __doc__——metrics wrapper 的 functools.wraps 会
    覆盖 __doc__，靠它作 description 来源的顺序很脆弱（I2）。
    """
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_search(
        query: str,
        search_depth: str = "basic",
        topic: str = "general",
        days: int | None = None,
        max_results: int = 5,
        include_answer: bool = False,
        include_raw_content: bool = False,
        include_images: bool = False,
    ) -> dict:
        return await tavily_search(query=query, search_depth=search_depth,
                                   topic=topic, days=days, max_results=max_results,
                                   include_answer=include_answer,
                                   include_raw_content=include_raw_content,
                                   include_images=include_images,
                                   pool=get_pool())

    mcp.tool(
        name="tavily_search",
        description=tavily_search.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("tavily_search")(_mcp_search))

    async def _mcp_extract(urls: list[str], extract_depth: str = "basic") -> dict:
        return await tavily_extract(urls=urls, extract_depth=extract_depth,
                                    pool=get_pool())

    mcp.tool(
        name="tavily_extract",
        description=tavily_extract.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("tavily_extract")(_mcp_extract))

    async def _mcp_crawl(urls: list[str], max_depth: int = 3, max_pages: int = 20,
                         max_cost: float = 10.0) -> dict:
        return await tavily_crawl(urls=urls, max_depth=max_depth,
                                  max_pages=max_pages, max_cost=max_cost,
                                  pool=get_pool())

    mcp.tool(
        name="tavily_crawl",
        description=tavily_crawl.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("tavily_crawl")(_mcp_crawl))

    async def _mcp_map(query: str, search_depth: str = "basic",
                       max_results: int = 100) -> dict:
        return await tavily_map(query=query, search_depth=search_depth,
                                max_results=max_results, pool=get_pool())

    mcp.tool(
        name="tavily_map",
        description=tavily_map.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("tavily_map")(_mcp_map))

    async def _mcp_research(query: str, max_depth: int = 3, max_learnings: int = 5,
                            max_sources: int = 5, max_browser_pages: int = 20) -> dict:
        return await tavily_research(query=query, max_depth=max_depth,
                                     max_learnings=max_learnings,
                                     max_sources=max_sources,
                                     max_browser_pages=max_browser_pages,
                                     pool=get_pool())

    mcp.tool(
        name="tavily_research",
        description=tavily_research.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("tavily_research")(_mcp_research))
