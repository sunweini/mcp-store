"""Tavily search tools.

Design note: functions at module level (not closures) so tests import
and call them directly with a FakePool. register() creates thin MCP
wrappers injecting the real pool.

Error/failover strategy (per spec 错误处理节):
- 幂等轻查询 (search/extract/map): 失败后换下一 key 重试一次
- 长任务 (crawl/research): 不重试 — 重跑成本高,失败留给用户决定
- client_factory 注入: 默认构造真实 TavilyClient;crawl/research 用 60s
  超时(长任务),其余 5s。测试注入 FakeClient 驱动公开方法,不依赖私有 _post。
"""
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

    async def _once(rec: dict) -> tuple:
        """Single attempt: returns (resp, exc); resp None on failure.

        finally 里 close：真实 TavilyClient 每次调用新建 httpx.AsyncClient，
        用完即关防连接泄漏。关闭方法名是 close()——aclose 是 httpx.
        AsyncClient 的方法，TavilyClient 上不存在（上轮错写 aclose，
        导致 getattr 恒为 None、关闭从未执行，连接泄漏依旧）。FakeClient
        无 close 时 getattr 兜底跳过。
        """
        client = factory(rec["key"], timeout)
        try:
            # endpoint 来自模块常量（非用户输入），getattr 安全；
            # TavilyClient 方法名与 endpoint 同名
            resp = await getattr(client, endpoint)(params)
            return resp, None
        except Exception as exc:
            return None, exc
        finally:
            closer = getattr(client, "close", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass

    key_rec = await pool.next_key()
    if key_rec is None:
        return {"status": "error",
                "message": "tavily 该源所有 API key 不可用，请在前台检查 key 池状态"}
    resp, exc = await _once(key_rec)
    if resp is not None:
        await pool.on_success(key_rec["key_id"])
        return {"status": "ok", "data": resp}
    kind = classify_error(exc, getattr(exc, "status_code", None))
    await pool.on_error(key_rec["key_id"], kind or ErrorKind.EXHAUSTED)
    if retryable:
        # 换下一 key 重试一次（幂等操作才允许）；next_key 已排除刚标记
        # 失败的 key，None 或同 key 均表示无可换 key
        key_rec2 = await pool.next_key()
        if key_rec2 and key_rec2["key_id"] != key_rec["key_id"]:
            resp2, exc2 = await _once(key_rec2)
            if resp2 is not None:
                await pool.on_success(key_rec2["key_id"])
                return {"status": "ok", "data": resp2}
            kind2 = classify_error(exc2, getattr(exc2, "status_code", None))
            await pool.on_error(key_rec2["key_id"], kind2 or ErrorKind.EXHAUSTED)
    return {"status": "error", "message": str(exc)}


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
