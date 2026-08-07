"""Brave search tools — web + local search.

Design note: functions at module level (not closures) so tests import
and call them directly with a FakePool. register() creates thin MCP
wrappers injecting the real pool.

Error/failover strategy (per spec 错误处理节):
- 两个工具都是幂等 GET 查询,失败后换下一 key 重试一次
- client_factory 注入: 默认构造真实 BraveClient(5s 超时),测试注入
  FakeClient 驱动公开方法,不依赖私有 _get
"""
from typing import Callable, Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
import structlog

from brave_client import BraveClient, classify_error

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

    async def _once(rec: dict) -> tuple:
        """Single attempt: returns (resp, exc); resp None on failure.

        finally 里 close：真实 BraveClient 每次调用新建 httpx.AsyncClient，
        用完即关防连接泄漏。关闭方法名是 close()——aclose 是 httpx.
        AsyncClient 的方法，BraveClient 上不存在（tavily 上轮错写 aclose
        导致 getattr 恒为 None、关闭从未执行；此处直接保持 close）。
        FakeClient 无 close 时 getattr 兜底跳过。
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
            if kind2:
                await pool.on_error(key_rec2["key_id"], kind2)
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
