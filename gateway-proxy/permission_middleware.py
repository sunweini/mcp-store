"""FastMCP Middleware subclass that enforces token auth + permissions on tools/call.

This is the wiring layer between FastMCP's middleware pipeline and the helper
functions in middleware.py (check_call_permission, classify_error,
record_call_failure). It intercepts every tools/call request:

1. Extracts the Bearer token from HTTP headers (get_http_headers).
2. Verifies the token against Redis (auth.verify_token).
3. Checks the token grants (server, mode) access (check_call_permission).
4. If denied -> records an audit failure + increments metrics + raises ToolError.
5. If allowed -> calls the backend; on exception classifies + records the failure.

Non-tools/call requests (tools/list, ping, initialize) pass through untouched -
the gateway returns an empty tool list for unauthenticated clients, which is
by design: clients need to discover the gateway before they can call anything.
"""
import time
import uuid

import structlog
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware, MiddlewareContext
from opentelemetry import trace

from auth import verify_token
from middleware import check_call_permission, classify_error, record_call_failure
# CRITICAL: import the module (not `from observability import ...`) so that
# attribute access resolves at CALL TIME, picking up the post-init_telemetry()
# values. A `from` import snapshots the names at import time (all None, because
# init_telemetry() hasn't run yet) and never sees the rebound instruments.
import observability

logger = structlog.get_logger()


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


class PermissionMiddleware(Middleware):
    """Intercept tools/call: verify token, check permission, audit failures.

    Only tools/call is intercepted - tools/list and other methods pass through
    without auth so clients can discover the gateway before authenticating.
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
            # Record the failure audit (journey stops at 'auth' or 'route').
            await record_call_failure(
                token_info=token_info,
                mcp_name=tool_name,
                error_type=error_type,
                message=f"Denied: {tool_name}",
                latency_ms=latency_ms,
                trace_id=trace_id,
                fail_stage="auth" if error_type == "invalid_token" else "route",
            )
            # Metrics: count the auth failure + record latency.
            if observability.AUTH_FAILURES:
                observability.AUTH_FAILURES.add(1, {"error_type": error_type})
            if observability.REQUESTS_TOTAL:
                observability.REQUESTS_TOTAL.add(1, {"status": "denied"})
            if observability.REQUEST_LATENCY:
                observability.REQUEST_LATENCY.record(latency_ms / 1000.0)

            raise ToolError(f"Permission denied: {error_type}")

        # ── Call the backend ────────────────────────────────────────
        try:
            result = await call_next(context)
        except Exception as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            err_type = classify_error(exc)
            await record_call_failure(
                token_info=token_info,
                mcp_name=tool_name,
                error_type=err_type,
                message=str(exc),
                latency_ms=latency_ms,
                trace_id=trace_id,
                fail_stage=tool_name.split("_", 1)[0] if "_" in tool_name else "backend",
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

        return result
