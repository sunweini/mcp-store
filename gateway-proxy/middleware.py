"""MCP middleware: permission enforcement + failure audit + metrics.

PermissionMiddleware runs on every tools/call: verifies the token, parses
the namespace prefix, and checks read/write. Denied calls are recorded as
audit failures and never reach the backend.

NOTE: FastMCP middleware uses on_message(context, call_next). The token is
read from the Authorization header (parsed in server.py and stashed on the
context); here we consume the already-verified token_info.
"""
import time
import httpx

from auth import check_permission
from routing import resolve_target, split_prefix, UnknownServerError
from audit import record_failure, record_call


def check_call_permission(token_info: dict | None, mcp_name: str) -> tuple[bool, str | None]:
    """Check whether a token may call a namespaced tool.

    Returns (allowed, error_type). error_type is one of the audit enum or None.
    """
    if token_info is None:
        return False, "invalid_token"
    try:
        server, tool, mode = resolve_target(mcp_name)
    except (ValueError, UnknownServerError):
        return False, "permission_denied"
    if not check_permission(token_info, server, mode):
        return False, "permission_denied"
    return True, None


def classify_error(exc: Exception) -> str:
    """Map an exception to an audit error_type enum value."""
    if isinstance(exc, httpx.TimeoutException):
        return "upstream_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection_error"
    return "upstream_error"


def build_journey(fail_stage: str, server: str, latency_ms: int) -> list[dict]:
    """Build the request journey: stages before fail_stage are ok, the fail
    stage carries the total latency, and stages after it were never reached
    (skip).

    NOTE: 独立成函数是因为失败面板数据源统一到 MySQL calls 表后，
    on_call_tool 需自行构建 journey 写入 calls 行，与 record_call_failure
    写 Redis 流的轨迹共用同一套 stage 推演逻辑，保证两处一致。
    """
    stages = ["client", "gateway", "auth", "route", server or "backend"]
    journey = []
    for i, st in enumerate(stages):
        if st == fail_stage:
            journey.append({"stage": st, "state": "fail", "ms": latency_ms})
            # subsequent stages were not reached
            for after in stages[i + 1:]:
                journey.append({"stage": after, "state": "skip", "ms": 0})
            break
        journey.append({"stage": st, "state": "ok", "ms": 0})
    return journey


async def record_call_failure(
    token_info: dict | None,
    mcp_name: str,
    error_type: str,
    message: str,
    latency_ms: int,
    trace_id: str,
    fail_stage: str,
) -> None:
    """Build a journey and write a failure audit record.

    fail_stage: where it broke - 'auth', 'route', or the backend server name.
    """
    server = ""
    tool = ""
    op = "read"
    # server/tool 用 split_prefix 解析而非 resolve_target：后者依赖
    # TOOL_REGISTRY，server 禁用（unmount）后已从 registry 卸载，会抛
    # UnknownServerError 导致审计字段落空——禁用后的调用恰是最需要审计的。
    # 前缀解析只按第一个 _ 切分（server 名禁下划线），不查 registry。
    try:
        server, tool = split_prefix(mcp_name)
    except ValueError:
        pass
    # op 仍尽力从 resolve_target 取（成功路径能拿到真实 read/write），
    # 未注册时降级默认 read，审计不因 registry 缺失而空
    try:
        _, _, op = resolve_target(mcp_name)
    except (ValueError, UnknownServerError):
        pass

    journey = build_journey(fail_stage, server, latency_ms)

    # Include the token name in the audit meta so the admin UI can show
    # which credential was used (or "(anonymous)" for missing tokens).
    # trace_id ties the audit record to the OTel span for cross-referencing.
    token_name = token_info.get("name", "(anonymous)") if token_info else "(anonymous)"

    await record_failure(
        journey=journey,
        error_type=error_type,
        meta={
            "trace_id": trace_id,
            "server": server,
            "tool": tool,
            "op": op,
            "message": message,
            "latency_ms": latency_ms,
            "token_name": token_name,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def record_call_audit(
    token_info: dict | None,
    mcp_name: str,
    latency_ms: int,
    trace_id: str,
    status: str,
    error_type: str | None = None,
    message: str | None = None,
    journey: list | None = None,
) -> None:
    """写全量调用明细到 MySQL calls 表（成功+失败均写）。

    与 record_call_failure 互补：failures 流（Redis）保留双写供回滚，
    calls 表（MySQL）是请求日志页 + 聚合统计 + 失败面板的统一数据源。
    message/journey 由 on_call_tool 失败路径传入，成功行留空。
    """
    server, tool, op = "", "", "read"
    # 同 record_call_failure：server/tool 用 split_prefix 而非 resolve_target，
    # 保证 server 禁用后拒绝/异常调用仍能记录真实的 server/tool 字段
    try:
        server, tool = split_prefix(mcp_name)
    except ValueError:
        pass
    # op 尽力从 resolve_target 取，registry 缺失（禁用）时降级默认 read
    try:
        _, _, op = resolve_target(mcp_name)
    except (ValueError, UnknownServerError):
        pass
    token_name = token_info.get("name", "(anonymous)") if token_info else "(anonymous)"
    await record_call(
        meta={
            "trace_id": trace_id,
            "server": server,
            "tool": tool,
            "op": op,
            "token_name": token_name,
            "latency_ms": latency_ms,
            "time": time.strftime("%Y-%m-%d %H:%M:%S.000", time.gmtime()),
        },
        status=status,
        error_type=error_type,
        message=message,
        journey=journey,
    )
