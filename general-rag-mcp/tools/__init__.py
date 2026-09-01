"""Tool registration module.

knowledge_base 子模块导出 register(mcp, get_client) 注册工具。
get_client: callable 返回 RagClient（模块级懒加载单例，stateless 模式）。

Metrics: 工具层指标（requests_total / duration / errors / in_flight）
由 register() 应用的 decorator 记录。
"""
import time
import functools

from tools import knowledge_base

try:
    from telemetry import REQUESTS_TOTAL, REQUEST_DURATION, ERRORS_TOTAL, IN_FLIGHT_REQUESTS
except ImportError:
    REQUESTS_TOTAL = REQUEST_DURATION = ERRORS_TOTAL = IN_FLIGHT_REQUESTS = None


def _metrics_wrapper(tool_name: str):
    """Decorator that records tool-level Prometheus metrics.

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


def register_tools(mcp, get_client) -> None:
    """Register all general-rag tools on the FastMCP server instance."""
    knowledge_base.register(mcp, get_client, metrics=_metrics_wrapper)
