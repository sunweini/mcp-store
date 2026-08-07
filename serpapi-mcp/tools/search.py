"""SerpAPI search tools — 5 engines (google/bing/baidu/duckduckgo/ebay).

Design note: functions at module level (not closures) so tests import
and call them directly with a FakePool. register() creates thin MCP
wrappers injecting the real pool.

Error/failover strategy (per spec 错误处理节):
- 5 引擎全是幂等 GET 查询，失败后换下一 key 重试一次（serpapi 无长任务）
- serpapi 欠费返回 200 + error body（非 4xx）——classify_error 是
  三参数版，此处必须把 resp.text 传进去才能判 EXHAUSTED（tavily/brave
  是两参数版，正文判据不同）
- client_factory 注入: 默认构造真实 SerpapiClient(5s 超时),测试注入
  FakeClient 驱动公开方法,不依赖私有 _http

Concurrency (spec C2/C3, Task 6):
- 借用语义：next_key 扣减 in-flight 防并发扎堆同一 key（429 风暴）；
  成功/可归类失败由 on_success/on_error 归还，瞬时错误（不记账路径）
  显式 release() 归还——借用必须对称，否则超时每次泄漏 +1
- 429 退避：幂等操作 429 时 sleep(0.5s) 再换 key（冷却 key 已被
  next_key 排除，不立即重打）
- per-endpoint semaphore：5 引擎都是轻查询（≤20），防并发打爆
  外部 API（主要防压测扎堆）
"""
import asyncio
from typing import Callable, Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
import structlog

from key_pool import ErrorKind
from serpapi_client import SerpapiClient, classify_error

logger = structlog.get_logger()

# 10s 而非 tavily/brave 的 5s：serpapi 聚合多引擎（google 聚合搜索
# 等），实测直连 2.2s、MCP 路径更慢——5s 太紧,正常请求易触发
# ReadTimeout,而超时属瞬时问题不写 key 状态,结果只是白等一轮
DEFAULT_TIMEOUT = 10.0

# 单一来源：工具名 → engine 参数（即 SerpAPI 的 engine 值）。5 引擎全是
# 幂等 GET，统一 retryable=True。SerpapiClient.search(engine, params) 的
# engine 由 client 写入 query；工具层只负责透传业务参数。
TOOLS: dict[str, dict] = {
    "serpapi_google": {"engine": "google", "retryable": True},
    "serpapi_bing": {"engine": "bing", "retryable": True},
    "serpapi_baidu": {"engine": "baidu", "retryable": True},
    "serpapi_duckduckgo": {"engine": "duckduckgo", "retryable": True},
    "serpapi_ebay": {"engine": "ebay", "retryable": True},
}

# client_factory(key, timeout) -> SerpapiClient 兼容对象（公开方法
# search(engine, params)）。默认造真实 client；测试注入 FakeClient。
ClientFactory = Callable[[str, float], object]

# ── per-endpoint 并发上限（spec C2，Task 6）────────────────────────
# 5 引擎全是幂等轻查询；上限 20 防并发打爆外部 API（主要防压测扎堆）。
# 模块级 dict 惰性建 semaphore（工具层单例，测试不触真实并发路径）。
_RATE_LIMIT_BACKOFF = 0.5   # 429 重试退避起点（秒）
_TOOL_CONCURRENCY = {
    "serpapi_google": 20,
    "serpapi_bing": 20,
    "serpapi_baidu": 20,
    "serpapi_duckduckgo": 20,
    "serpapi_ebay": 20,
}
_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_semaphore(tool_name: str) -> asyncio.Semaphore:
    sem = _semaphores.get(tool_name)
    if sem is None:
        sem = asyncio.Semaphore(_TOOL_CONCURRENCY[tool_name])
        _semaphores[tool_name] = sem
    return sem


def _default_factory(key: str, timeout: float) -> SerpapiClient:
    return SerpapiClient(key, timeout=timeout)


async def _call_with_pool(pool, tool_name: str, params: dict,
                          client_factory: Optional[ClientFactory] = None) -> dict:
    """Pick key → call API → report result to pool. One retry on failover.

    Returns the tool response dict (status ok/error).
    client_factory: (key, timeout) -> client。默认真实 SerpapiClient;
    测试注入 FakeClient。engine/重试策略均取自 TOOLS 表。

    重试可行性由 next_key() 语义承载（不触碰 pool 私有属性）：
    失败 key 已被 on_error 标记（invalid/exhausted 永久跳过、cooldown
    冷却中），next_key 返回 None 或同一 key 即表示没有可换的 key。
    """
    cfg = TOOLS[tool_name]
    engine = cfg["engine"]
    retryable = cfg["retryable"]
    factory = client_factory or _default_factory
    sem = _get_semaphore(tool_name)

    async def _once(rec: dict) -> tuple:
        """Single attempt: returns (resp, exc); resp None on failure.

        finally 里 close：真实 SerpapiClient 每次调用新建
        httpx.AsyncClient，用完即关防连接泄漏。关闭方法名是 close()——
        aclose 是 httpx.AsyncClient 的方法，SerpapiClient 上不存在
        （tavily 上轮错写 aclose 导致 getattr 恒为 None、关闭从未执行）。
        FakeClient 无 close 时 getattr 兜底跳过。
        """
        client = factory(rec["key"], DEFAULT_TIMEOUT)
        try:
            resp = await client.search(engine, params)
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

    def _classify(exc: Exception) -> ErrorKind | None:
        # serpapi 三参数 classify_error：EXHAUSTED 靠 body 文本判（200 +
        # "account has exceeded quota" 类 error body）。SerpapiError.detail
        # 存的是截断 body（<=200 字符）——关键词落在截断区外会被漏判，
        # 但欠费 body 的关键词都在开头（实测 error 字段在前），可接受。
        body = getattr(exc, "detail", "") or ""
        return classify_error(exc, getattr(exc, "status_code", None), body)

    # semaphore 防并发打爆外部 API（spec C2）：acquire 在取 key 之前——
    # 超过并发上限的请求排队等 semaphore 而非挤占 key 池借用
    async with sem:
        key_rec = await pool.next_key()
        if key_rec is None:
            return {"status": "error",
                    "message": "serpapi 该源所有 API key 不可用，请在前台检查 key 池状态"}
        resp, exc = await _once(key_rec)
        if resp is not None:
            await pool.on_success(key_rec["key_id"])
            return {"status": "ok", "data": resp}
        kind = _classify(exc)
        # 仅可归类错误才写 key 状态（实测超时/连接错误 classify_error 返回
        # None——瞬时问题,key 本身有效,写 EXHAUSTED 会把好 key 永久剔除,
        # 曾致一次 ReadTimeout 杀掉全部 key）
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
                resp2, exc2 = await _once(key_rec2)
                if resp2 is not None:
                    await pool.on_success(key_rec2["key_id"])
                    return {"status": "ok", "data": resp2}
                kind2 = _classify(exc2)
                if kind2:
                    await pool.on_error(key_rec2["key_id"], kind2)
                else:
                    await pool.release(key_rec2["key_id"])
    # 最终失败消息只含 status + 截断 body（SerpapiError.detail 是
    # resp.text 截断，安全）——不落 str(exc)：网络异常（httpx.HTTPError）
    # 的 repr 带完整请求 URL，serpapi 的 api_key 在 query 里，str(exc)
    # 会把明文 key 带进工具返回体（评审 I-1，与 client 层日志同防线）
    status = getattr(exc, "status_code", None)
    detail = getattr(exc, "detail", "") or ""
    if status is not None:
        return {"status": "error", "message": f"serpapi error {status}: {detail[:200]}"}
    return {"status": "error", "message": "serpapi 请求失败（网络/超时），请稍后重试"}


async def serpapi_google(query: str, gl: str | None = None, hl: str | None = None,
                         num: int = 10, start: int = 0, *, pool,
                         client_factory: Optional[ClientFactory] = None) -> dict:
    """Google web search via SerpAPI. Returns organic_results (title/link/snippet).

    query: 搜索词。gl/hl: 国家/语言代码（如 us/en）。num: 返回结果数 1-100。
    start: 分页起始位置（0-based，每页 num 条）。
    """
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    params = {"q": query, "num": min(max(num, 1), 100), "start": max(start, 0)}
    if gl:
        params["gl"] = gl
    if hl:
        params["hl"] = hl
    return await _call_with_pool(pool, "serpapi_google", params,
                                 client_factory=client_factory)


async def serpapi_bing(query: str, gl: str | None = None, hl: str | None = None,
                       cc: str | None = None, count: int = 10, *, pool,
                       client_factory: Optional[ClientFactory] = None) -> dict:
    """Bing web search via SerpAPI. Returns organic_results.

    query: 搜索词。gl/hl/cc: 国家/语言/地区代码（如 us/en/us）。count: 1-100。
    """
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    params = {"q": query, "count": min(max(count, 1), 100)}
    if gl:
        params["gl"] = gl
    if hl:
        params["hl"] = hl
    if cc:
        params["cc"] = cc
    return await _call_with_pool(pool, "serpapi_bing", params,
                                 client_factory=client_factory)


async def serpapi_baidu(query: str, cti: str | None = None, page_num: int = 1, *,
                        pool, client_factory: Optional[ClientFactory] = None) -> dict:
    """Baidu web search via SerpAPI. Returns organic_results.

    query: 搜索词。cti: 时间过滤（1=24h/2=一周/3=一月/4=一年）。page_num: 页码。
    """
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    params = {"q": query, "page_num": max(page_num, 1)}
    if cti:
        params["cti"] = cti
    return await _call_with_pool(pool, "serpapi_baidu", params,
                                 client_factory=client_factory)


async def serpapi_duckduckgo(query: str, kl: str | None = None, *, pool,
                             client_factory: Optional[ClientFactory] = None) -> dict:
    """DuckDuckGo web search via SerpAPI. Returns organic_results.

    query: 搜索词。kl: 区域语言代码（如 us-en、zh-cn，留空用默认）。
    """
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    params = {"q": query}
    if kl:
        params["kl"] = kl
    return await _call_with_pool(pool, "serpapi_duckduckgo", params,
                                 client_factory=client_factory)


async def serpapi_ebay(_nkw: str, ebay_domain: str = "ebay.com", *, pool,
                       client_factory: Optional[ClientFactory] = None) -> dict:
    """eBay product search via SerpAPI. Returns shopping_results.

    _nkw: 商品关键词（eBay 官方参数名）。ebay_domain: 站点域名
    （ebay.com / ebay.co.uk / ebay.de 等）。
    """
    if not _nkw.strip():
        return {"status": "error", "message": "_nkw 不能为空"}
    params = {"_nkw": _nkw, "ebay_domain": ebay_domain}
    return await _call_with_pool(pool, "serpapi_ebay", params,
                                 client_factory=client_factory)


def register(mcp: FastMCP, get_pool, metrics=None) -> None:
    """Register all serpapi tools. get_pool: callable returning the KeyPool.

    Note: 每个工具用显式具名包装而非 *args 泛型包装——FastMCP v4
    不支持 *args 工具函数（ParsedFunction 校验拒绝），参数必须显式
    声明才能生成正确 inputSchema。pool/client_factory 是注入参数，
    不暴露给 MCP client（不出现在 schema）。

    description 的单一来源是 mcp.tool(description=...) 的显式参数，
    不依赖包装函数 __doc__——metrics wrapper 的 functools.wraps 会
    覆盖 __doc__，靠它作 description 来源的顺序很脆弱。
    """
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_google(query: str, gl: str | None = None, hl: str | None = None,
                          num: int = 10, start: int = 0) -> dict:
        return await serpapi_google(query=query, gl=gl, hl=hl, num=num, start=start,
                                    pool=get_pool())

    mcp.tool(
        name="serpapi_google",
        description=serpapi_google.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("serpapi_google")(_mcp_google))

    async def _mcp_bing(query: str, gl: str | None = None, hl: str | None = None,
                        cc: str | None = None, count: int = 10) -> dict:
        return await serpapi_bing(query=query, gl=gl, hl=hl, cc=cc, count=count,
                                  pool=get_pool())

    mcp.tool(
        name="serpapi_bing",
        description=serpapi_bing.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("serpapi_bing")(_mcp_bing))

    async def _mcp_baidu(query: str, cti: str | None = None, page_num: int = 1) -> dict:
        return await serpapi_baidu(query=query, cti=cti, page_num=page_num,
                                   pool=get_pool())

    mcp.tool(
        name="serpapi_baidu",
        description=serpapi_baidu.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("serpapi_baidu")(_mcp_baidu))

    async def _mcp_duckduckgo(query: str, kl: str | None = None) -> dict:
        return await serpapi_duckduckgo(query=query, kl=kl, pool=get_pool())

    mcp.tool(
        name="serpapi_duckduckgo",
        description=serpapi_duckduckgo.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("serpapi_duckduckgo")(_mcp_duckduckgo))

    async def _mcp_ebay(_nkw: str, ebay_domain: str = "ebay.com") -> dict:
        return await serpapi_ebay(_nkw=_nkw, ebay_domain=ebay_domain, pool=get_pool())

    mcp.tool(
        name="serpapi_ebay",
        description=serpapi_ebay.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("serpapi_ebay")(_mcp_ebay))
