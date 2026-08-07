"""FastMCP Middleware subclass that enforces token auth + permissions on tools/call.

This is the wiring layer between FastMCP's middleware pipeline and the helper
functions in middleware.py (check_call_permission, classify_error,
build_audit_meta). It intercepts every tools/call request:

1. Extracts the Bearer token from HTTP headers (get_http_headers).
2. Verifies the token against Redis (auth.verify_token).
3. Checks the token grants (server, mode) access (check_call_permission).
4. If denied -> records an audit XADD (audit.record_call_stream) + increments
   metrics + raises ToolError.
5. If allowed -> calls the backend; on exception classifies + records the failure.
6. On success -> records the audit XADD with status=ok (full call detail).

tools/list is filtered by token permissions (on_list_tools): anonymous or
invalid tokens get an empty list; valid tokens see only the tools their
(server, mode) permissions grant. ping and initialize still pass through
untouched so health checks work without auth.
"""
import asyncio
import inspect
import os
import time
import uuid

import structlog
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from opentelemetry import trace

from auth import verify_token, check_permission
from middleware import (
    build_journey,
    build_audit_meta,
    check_call_permission,
    classify_error,
)
from audit import record_call_stream
from routing import resolve_target, split_prefix, UnknownServerError
# CRITICAL: import the module (not `from observability import ...`) so that
# attribute access resolves at CALL TIME, picking up the post-init_telemetry()
# values. A `from` import snapshots the names at import time (all None, because
# init_telemetry() hasn't run yet) and never sees the rebound instruments.
import observability

logger = structlog.get_logger()

# ── Task 4: 背压（per-server semaphore）+ 总超时（wait_for）────────
# 并发控制：semaphore 上限超出时排队而非打爆后端（后端连接池容量有限，
# 无界并发会让后端 TCP 队列积压，拖垮全部请求）。
# 总超时：单请求最长允许时长——后端长任务（tavily research 60s）之上加
# 余量，默认 90s。绝不能 30s（会把长任务全部掐死）。
_BACKEND_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_BACKEND_SEMAPHORE_LIMIT = int(os.environ.get("BACKEND_SEMAPHORE_LIMIT", "100"))
_CALL_TIMEOUT_DEFAULT = 90.0


def _get_semaphore(server: str) -> asyncio.Semaphore:
    """返回 per-server 信号量（懒创建，线程内单例）。"""
    sem = _BACKEND_SEMAPHORES.get(server)
    if sem is None:
        sem = asyncio.Semaphore(_BACKEND_SEMAPHORE_LIMIT)
        _BACKEND_SEMAPHORES[server] = sem
    return sem


def _get_call_timeout(server: str) -> float:
    """返回 server 的总超时秒数。

    per-server 覆盖从 registry._mounted_timeouts 读（挂载/更新时从
    servers:{name} hash 缓存，请求路径不读 Redis——每请求读 Redis 会
    放大请求路径延迟）。缺省 90s。
    """
    try:
        from registry import _mounted_timeouts
        return _mounted_timeouts.get(server, _CALL_TIMEOUT_DEFAULT)
    except ImportError:
        return _CALL_TIMEOUT_DEFAULT


def _extract_token(headers: dict[str, str] | None) -> str | None:
    """Pull the Bearer token from the Authorization header dict.

    get_http_headers returns lowercased keys, so we look up 'authorization'.
    """
    if not headers:
        return None
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _current_trace_id() -> str:
    """Return the OTel trace_id (hex) if a span is active, else a UUID fallback.

    The fallback ensures every audit record has a non-empty trace_id even when
    OTel is not configured (e.g. in unit tests), so the admin UI can always
    link a failure to a request.
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx and ctx.is_valid:
        return f"{ctx.trace_id:032x}"
    return uuid.uuid4().hex


def _resolve_server_name(mcp_name: str) -> str:
    """Best-effort 解析工具名对应的 server 名，解析失败返回 ""。

    供 build_journey 决定轨迹末段 stage 名（真实 server 名或回退 "backend"），
    与 build_audit_meta 内部的 resolve 容错语义保持一致。
    """
    # split_prefix 只按第一个 _ 切分，不依赖 TOOL_REGISTRY：server 禁用
    # （unmount）后也能解析出真实 server 名，与 build_audit_meta 同步
    # （record_call_stream 收到的 journey 用同一 server 名，两处 stage 一致）
    try:
        server, _ = split_prefix(mcp_name)
        return server
    except ValueError:
        return ""


class PermissionMiddleware(Middleware):
    """Intercept tools/call: verify token, check permission, audit failures.

    Intercepts tools/call (auth + permission + audit) and filters tools/list
    by token permissions. ping/initialize pass through for unauthenticated
    health checks.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext,
        call_next,
    ):
        tool_name = context.message.name
        start = time.monotonic()
        trace_id = _current_trace_id()

        # ── Extract + verify token ──────────────────────────────────
        # NOTE: get_http_headers() excludes 'authorization' by default.
        # Must pass include={"authorization"} to get the Bearer token.
        headers = get_http_headers(include={"authorization"})
        token = _extract_token(headers)

        # verify_token can raise KeyError/JSONDecodeError on malformed Redis
        # data (corrupted hash, truncated JSON). Treat any exception as an
        # invalid token so a Redis glitch does not crash the request path.
        token_info = None
        if token:
            try:
                token_info = await verify_token(token)
            except Exception as exc:
                logger.warning(
                    "verify_token_exception",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    service="gateway-proxy",
                )
                token_info = None

        # ── Permission check ────────────────────────────────────────
        allowed, error_type = check_call_permission(token_info, tool_name)

        if not allowed:
            latency_ms = int((time.monotonic() - start) * 1000)
            fail_stage = "auth" if error_type == "invalid_token" else "route"
            message = f"Denied: {tool_name}"
            # 单次 XADD 记录失败审计：message + journey 进 stream，
            # 消费者落 MySQL 时失败面板的「错误信息 / 查看轨迹」直接可用
            await record_call_stream(
                meta=build_audit_meta(token_info, tool_name, latency_ms, trace_id),
                status="fail",
                error_type=error_type,
                message=message,
                journey=build_journey(fail_stage, _resolve_server_name(tool_name), latency_ms),
            )
            # Metrics: count the auth failure + record latency.
            if observability.AUTH_FAILURES:
                observability.AUTH_FAILURES.add(1, {"error_type": error_type})
            if observability.REQUESTS_TOTAL:
                observability.REQUESTS_TOTAL.add(1, {"status": "denied"})
            if observability.REQUEST_LATENCY:
                observability.REQUEST_LATENCY.record(latency_ms / 1000.0)

            raise ToolError(f"Permission denied: {error_type}")

        # ── Call the backend（背压 + 总超时）───────────────────────
        # Task 4: 并发超出 semaphore 上限时排队（防止打爆后端连接池）；
        # wait_for 掐断超时请求（后端挂死不能无限拖住调用方）。
        # 为什么 wait_for 不包 semaphore acquire：排队时间不计入 90s
        # 总超时——后端挂死 + 队列满时，排队请求的 deadline 由前端
        # client 超时兜底（审查 Finding 2 定案）。只掐 semaphore 持有
        # 期间（即真正打到后端）的请求，避免前端重试风暴二次压垮队列。
        server = _resolve_server_name(tool_name)
        sem = _get_semaphore(server or "unknown")
        timeout = _get_call_timeout(server)
        async with sem:
            try:
                result = await asyncio.wait_for(call_next(context), timeout=timeout)
            except asyncio.TimeoutError as exc:
                latency_ms = int((time.monotonic() - start) * 1000)
                # 超时计入审计（upstream_timeout），与 httpx 超时同分类
                message = f"Backend timeout after {timeout}s"
                await record_call_stream(
                    meta=build_audit_meta(token_info, tool_name, latency_ms, trace_id),
                    status="fail",
                    error_type="upstream_timeout",
                    message=message,
                    journey=build_journey(server or "backend", server, latency_ms),
                )
                if observability.REQUESTS_TOTAL:
                    observability.REQUESTS_TOTAL.add(1, {"status": "error"})
                if observability.REQUEST_LATENCY:
                    observability.REQUEST_LATENCY.record(latency_ms / 1000.0)
                raise ToolError(message) from exc
            except Exception as exc:
                latency_ms = int((time.monotonic() - start) * 1000)
                err_type = classify_error(exc)
                fail_stage = tool_name.split("_", 1)[0] if "_" in tool_name else "backend"
                message = str(exc)
                await record_call_stream(
                    meta=build_audit_meta(token_info, tool_name, latency_ms, trace_id),
                    status="fail",
                    error_type=err_type,
                    message=message,
                    journey=build_journey(fail_stage, _resolve_server_name(tool_name), latency_ms),
                )
                if observability.REQUESTS_TOTAL:
                    observability.REQUESTS_TOTAL.add(1, {"status": "error"})
                if observability.REQUEST_LATENCY:
                    observability.REQUEST_LATENCY.record(latency_ms / 1000.0)
                raise

        latency_ms = int((time.monotonic() - start) * 1000)
        if observability.REQUESTS_TOTAL:
            observability.REQUESTS_TOTAL.add(1, {"status": "ok"})
        if observability.REQUEST_LATENCY:
            observability.REQUEST_LATENCY.record(latency_ms / 1000.0)

        # 成功也写流：请求日志页需要全量调用明细（不止失败）
        await record_call_stream(
            meta=build_audit_meta(token_info, tool_name, latency_ms, trace_id),
            status="ok",
            error_type=None,
            message="",
            journey=[],
        )

        return result

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        """Filter tools/list by token permissions (tool-level granularity).

        Anonymous/invalid token -> empty list. Otherwise a tool is visible
        iff the token grants its (server, mode). Mode comes from the same
        TOOL_REGISTRY that tools/call checks, so list visibility and call
        permission never diverge.
        """
        # call_next is async in production FastMCP but unit tests inject a
        # sync lambda; normalize both so filtering logic stays identical.
        tools = call_next(context)
        if inspect.isawaitable(tools):
            tools = await tools

        # NOTE: get_http_headers() excludes 'authorization' by default.
        # Must pass include={"authorization"} to get the Bearer token
        # (same pitfall as on_call_tool above).
        token = _extract_token(get_http_headers(include={"authorization"}))

        # Same guard as on_call_tool: malformed Redis data (corrupted hash,
        # truncated JSON) must not crash tools/list; treat any exception as
        # invalid token -> empty list below.
        token_info = None
        if token:
            try:
                token_info = await verify_token(token)
            except Exception as exc:
                logger.warning(
                    "verify_token_exception",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    service="gateway-proxy",
                )
                token_info = None

        if token_info is None:
            # 空清单而非报错：client 能连通但看不到工具，不泄露名称/描述，
            # 也避免 list 报错引发 client 断连/重试风暴
            return []

        visible = []
        for t in tools:
            try:
                server, _tool, mode = resolve_target(t.name)
            except (ValueError, UnknownServerError):
                # ValueError = 无下划线前缀，UnknownServerError = 未注册前缀。
                # 来源无法确定时安全默认不列出（与 check_call_permission 的
                # 捕获语义对齐，避免 tools/list 因畸形工具名 500）
                continue
            if check_permission(token_info, server, mode):
                visible.append(t)
        return visible
