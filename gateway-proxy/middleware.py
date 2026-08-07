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

    NOTE: 独立成函数是因为 on_call_tool 的三条路径（拒绝/异常/成功）共用同一套
    stage 推演逻辑构建轨迹，保证各路径写入 audit:calls 的 journey 结构一致。
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


def build_audit_meta(
    token_info: dict | None,
    mcp_name: str,
    latency_ms: int,
    trace_id: str,
) -> dict:
    """Build the audit meta dict (time/server/tool/op/token_name/latency_ms/trace_id).

    server/tool 用 split_prefix 解析而非 resolve_target：后者依赖
    TOOL_REGISTRY，server 禁用（unmount）后已从 registry 卸载，会抛
    UnknownServerError 导致审计字段落空——禁用后的调用恰是最需要审计的。
    前缀解析只按第一个 _ 切分（server 名禁下划线），不查 registry。
    op 仍尽力从 resolve_target 取（成功路径能拿到真实 read/write），
    未注册时降级默认 read，审计不因 registry 缺失而空。

    token_name 让管理界面能展示用的是哪个凭证（无 token 时 "(anonymous)"）；
    trace_id 把审计记录与 OTel span 关联起来，便于跨引用。
    """
    server, tool, op = "", "", "read"
    try:
        server, tool = split_prefix(mcp_name)
    except ValueError:
        pass
    try:
        _, _, op = resolve_target(mcp_name)
    except (ValueError, UnknownServerError):
        pass
    token_name = token_info.get("name", "(anonymous)") if token_info else "(anonymous)"
    return {
        "trace_id": trace_id,
        "server": server,
        "tool": tool,
        "op": op,
        "token_name": token_name,
        "latency_ms": latency_ms,
        # time 格式锁死 %Y-%m-%d %H:%M:%S.000（固定 .000）：admin 消费者按此解析
        "time": time.strftime("%Y-%m-%d %H:%M:%S.000", time.gmtime()),
    }
