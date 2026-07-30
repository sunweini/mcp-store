"""MCP middleware: permission enforcement + failure audit + metrics.

PermissionMiddleware runs on every tools/call: verifies the token, parses
the namespace prefix, and checks read/write. Denied calls are recorded as
audit failures and never reach the backend.

NOTE: FastMCP middleware uses on_message(context, call_next). The token is
read from the Authorization header (parsed in server.py and stashed on the
context); here we consume the already-verified token_info.
"""
import time
import structlog
import httpx

from auth import verify_token, check_permission
from routing import resolve_target, UnknownServerError
from audit import record_failure

logger = structlog.get_logger()


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
    try:
        server, tool, op = resolve_target(mcp_name)
    except (ValueError, UnknownServerError):
        pass

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
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
