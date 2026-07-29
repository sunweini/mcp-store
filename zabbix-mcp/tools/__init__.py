"""Tool registration module.

Each sub-module exports a register(mcp, get_zabbix) function that attaches tools.
This keeps tool definitions isolated and testable independently.

get_zabbix: callable returning the ZabbixClient from app state.
Deferred lookup (closure) because the client is initialized in the lifespan,
not at import time.

Metrics: tool-level metrics (requests_total, duration, errors, in_flight)
are recorded by a decorator applied in each module's register().
"""
import time
import functools

from tools import problems, maintenance, events

# NOTE: metrics may be None if prometheus exporter not installed.
# All record calls are guarded with `if metric:` checks.
try:
    from telemetry import REQUESTS_TOTAL, REQUEST_DURATION, ERRORS_TOTAL, IN_FLIGHT_REQUESTS
except ImportError:
    REQUESTS_TOTAL = REQUEST_DURATION = ERRORS_TOTAL = IN_FLIGHT_REQUESTS = None


def _metrics_wrapper(tool_name: str):
    """Decorator that records tool-level Prometheus metrics.

    Wraps each tool function to track:
    - requests_total (counter)
    - request_duration_seconds (histogram)
    - errors_total (counter, on error status or exception)
    - in_flight_requests (gauge)
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            if REQUESTS_TOTAL:
                REQUESTS_TOTAL.add(1, attributes={"tool_name": tool_name})
            if IN_FLIGHT_REQUESTS:
                IN_FLIGHT_REQUESTS.add(1)

            start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                if isinstance(result, dict) and result.get("status") == "error":
                    if ERRORS_TOTAL:
                        ERRORS_TOTAL.add(1, attributes={"tool_name": tool_name, "error_type": "tool_error"})
                return result
            except Exception as e:
                if ERRORS_TOTAL:
                    ERRORS_TOTAL.add(1, attributes={"tool_name": tool_name, "error_type": type(e).__name__})
                raise
            finally:
                duration = time.monotonic() - start
                if REQUEST_DURATION:
                    REQUEST_DURATION.record(duration, attributes={"tool_name": tool_name})
                if IN_FLIGHT_REQUESTS:
                    IN_FLIGHT_REQUESTS.add(-1)
        return wrapper
    return decorator


def register_tools(mcp, get_zabbix) -> None:
    """Register all Zabbix tools on the FastMCP server instance."""
    problems.register(mcp, get_zabbix, metrics=_metrics_wrapper)
    maintenance.register(mcp, get_zabbix, metrics=_metrics_wrapper)
    events.register(mcp, get_zabbix, metrics=_metrics_wrapper)
