"""Brave search tools — web + local search.

Design note: functions at module level (not closures) so tests import
and call them directly with a FakePool. register() creates thin MCP
wrappers injecting the real pool.

Error/failover strategy (per spec 错误处理节):
- 两个工具都是幂等 GET 查询,失败后换下一 key 重试一次
- client_factory 注入: 默认构造真实 BraveClient(5s 超时),测试注入
  FakeClient 驱动公开方法,不依赖私有 _get

Concurrency (spec C2/C3, Task 6):
- 借用语义：next_key 扣减 in-flight 防并发扎堆同一 key（429 风暴）；
  成功/可归类失败由 on_success/on_error 归还，瞬时错误（不记账路径）
  显式 release() 归还——借用必须对称，否则超时每次泄漏 +1
- 429 退避：幂等操作 429 时 sleep(0.5s) 再换 key（冷却 key 已被
  next_key 排除，不立即重打）
- per-endpoint semaphore：两工具都是轻查询（≤20）
"""
import asyncio
from typing import Callable, Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
import structlog

from brave_client import BraveClient, classify_error
from key_pool import ErrorKind

logger = structlog.get_logger()

DEFAULT_TIMEOUT = 5.0

# 单一来源：工具名 → endpoint/参数上限/重试策略。endpoint 即 BraveClient
# 公开方法名（web_search/local_search）；两个工具都是幂等 GET,统一
# retryable=True（spec 错误处理节允许幂等操作换 key 重试一次）。
TOOLS: dict[str, dict] = {
    "brave_web_search": {"endpoint": "web_search", "retryable": True},
    "brave_local_search": {"endpoint": "local_search", "retryable": True},
}

# client_factory(key, timeout) -> BraveClient 兼容对象(公开方法
# web_search/local_search)。默认造真实 client;测试注入 FakeClient。
ClientFactory = Callable[[str, float], object]

# ── per-endpoint 并发上限（spec C2，Task 6）────────────────────────
# 两工具都是幂等轻查询；上限 20 防并发打爆外部 API（主要防压测扎堆）。
# 模块级 dict 惰性建 semaphore（工具层单例）。
_RATE_LIMIT_BACKOFF = 0.5   # 429 重试退避起点（秒）
_TOOL_CONCURRENCY = {
    "brave_web_search": 20,
    "brave_local_search": 20,
}
_semaphores: dict[str, asyncio.Semaphore] = {}


def _get_semaphore(tool_name: str) -> asyncio.Semaphore:
    sem = _semaphores.get(tool_name)
    if sem is None:
        sem = asyncio.Semaphore(_TOOL_CONCURRENCY[tool_name])
        _semaphores[tool_name] = sem
    return sem


def _default_factory(key: str, timeout: float) -> BraveClient:
    # Task 5：proxy 不再经 BraveClient 逐请求传——共享 client 在
    # get_shared_client() 里从 SEARCH_PROXY env 读定（生产必须走
    # 内网代理）。proxy 参数保留仅为兼容旧签名，transport 为 None 时
    # 实际走共享 client（含代理）
    return BraveClient(key, timeout=timeout)


async def _call_with_pool(pool, tool_name: str, params: dict,
                          client_factory: Optional[ClientFactory] = None) -> dict:
    """Pick key → call API → report result to pool. One retry on failover.

    Returns the tool response dict (status ok/error).
    client_factory: (key, timeout) -> client。默认真实 BraveClient;
    测试注入 FakeClient。timeout/endpoint/重试策略均取自 TOOLS 表。

    重试可行性由 next_key() 语义承载（不触碰 pool 私有属性——I1）：
    失败 key 已被 on_error 标记（invalid/exhausted 永久跳过、cooldown
    冷却中），next_key 返回 None 或同一 key 即表示没有可换的 key。
    """
    cfg = TOOLS[tool_name]
    endpoint = cfg["endpoint"]
    retryable = cfg["retryable"]
    factory = client_factory or _default_factory
    sem = _get_semaphore(tool_name)

    async def _once(rec: dict) -> tuple:
        """Single attempt: returns (resp, exc); resp None on failure.

        finally 里 close：真实 BraveClient 共享进程级 httpx 连接池
        （brave_client.get_shared_client，C1），close() 幂等——共享
        client 进程级存活不关闭（close 内部判断非共享才 aclose），
        测试注入的自建 client 由 close 归还有关权。FakeClient 无
        close 时 getattr 兜底跳过。
        """
        client = factory(rec["key"], DEFAULT_TIMEOUT)
        try:
            # endpoint 来自模块常量（非用户输入），getattr 安全；
            # BraveClient 方法名与 endpoint 同名
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

    # semaphore 防并发打爆外部 API（spec C2）：acquire 在取 key 之前——
    # 超过并发上限的请求排队等 semaphore 而非挤占 key 池借用
    async with sem:
        key_rec = await pool.next_key()
        if key_rec is None:
            return {"status": "error",
                    "message": "brave 该源所有 API key 不可用，请在前台检查 key 池状态"}
        resp, exc = await _once(key_rec)
        if resp is not None:
            await pool.on_success(key_rec["key_id"])
            return {"status": "ok", "data": resp}
        kind = classify_error(exc, getattr(exc, "status_code", None))
        # 仅可归类错误才写 key 状态（实测超时/连接错误 classify_error 返回
        # None——瞬时问题,key 本身有效,写 EXHAUSTED 会把好 key 永久剔除）
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
                kind2 = classify_error(exc2, getattr(exc2, "status_code", None))
                if kind2:
                    await pool.on_error(key_rec2["key_id"], kind2)
                else:
                    await pool.release(key_rec2["key_id"])
        return {"status": "error", "message": str(exc)}


async def brave_web_search(
    query: str,
    count: int = 10,
    offset: int = 0,
    *,
    pool,
    client_factory: Optional[ClientFactory] = None,
) -> dict:
    """Web search via Brave Search API. Returns web results with title/url/description.

    query: 搜索词。count: 返回结果数 1-20。offset: 分页偏移 0-9
    （Brave API 限制 offset 上限 9，超过直接报错）。
    """
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    if not (1 <= count <= 20):
        return {"status": "error", "message": "count 必须在 1-20 之间"}
    if not (0 <= offset <= 9):
        return {"status": "error", "message": "offset 必须在 0-9 之间"}
    params = {"q": query, "count": count, "offset": offset}
    return await _call_with_pool(pool, "brave_web_search", params,
                                 client_factory=client_factory)


async def brave_local_search(
    query: str,
    count: int = 5,
    *,
    pool,
    client_factory: Optional[ClientFactory] = None,
) -> dict:
    """Local (place) search via Brave Search API. Returns local business results.

    query: 搜索词（如 "pizza 北京 朝阳区"）。count: 返回结果数 1-20。
    """
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    if not (1 <= count <= 20):
        return {"status": "error", "message": "count 必须在 1-20 之间"}
    params = {"q": query, "count": count}
    return await _call_with_pool(pool, "brave_local_search", params,
                                 client_factory=client_factory)


def register(mcp: FastMCP, get_pool, metrics=None) -> None:
    """Register all brave tools. get_pool: callable returning the KeyPool.

    Note: 每个工具用显式具名包装而非 *args 泛型包装——FastMCP v4
    不支持 *args 工具函数（ParsedFunction 校验拒绝），参数必须显式
    声明才能生成正确 inputSchema。pool/client_factory 是注入参数，
    不暴露给 MCP client（不出现在 schema）。

    description 的单一来源是 mcp.tool(description=...) 的显式参数，
    不依赖包装函数 __doc__——metrics wrapper 的 functools.wraps 会
    覆盖 __doc__，靠它作 description 来源的顺序很脆弱（I2）。
    """
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_web_search(query: str, count: int = 10, offset: int = 0) -> dict:
        return await brave_web_search(query=query, count=count, offset=offset,
                                      pool=get_pool())

    mcp.tool(
        name="brave_web_search",
        description=brave_web_search.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("brave_web_search")(_mcp_web_search))

    async def _mcp_local_search(query: str, count: int = 5) -> dict:
        return await brave_local_search(query=query, count=count, pool=get_pool())

    mcp.tool(
        name="brave_local_search",
        description=brave_local_search.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("brave_local_search")(_mcp_local_search))
