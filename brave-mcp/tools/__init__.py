"""Tool registration — mirrors zabbix-mcp pattern."""
import time
import functools

from tools import web

try:
    from telemetry import SEARCH_REQUESTS_TOTAL, SEARCH_REQUEST_DURATION
except ImportError:
    SEARCH_REQUESTS_TOTAL = SEARCH_REQUEST_DURATION = None


def _metrics_wrapper(tool_name: str):
    """Record search_requests_total / duration（工具层只打这两个指标）。

    key 失效计数（search_key_invalid_total）由池层记账，工具层不感知，
    故此处不记录（评审 M1：死导入已删）。
    OBS-CORE-003: label 只含 provider/engine/status —— 低基数,无 key 维度。
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                if SEARCH_REQUESTS_TOTAL:
                    status = "success" if result.get("status") == "ok" else "error"
                    SEARCH_REQUESTS_TOTAL.add(
                        1, attributes={"provider": "brave", "engine": tool_name, "status": status})
                return result
            except Exception:
                if SEARCH_REQUESTS_TOTAL:
                    SEARCH_REQUESTS_TOTAL.add(
                        1, attributes={"provider": "brave", "engine": tool_name, "status": "error"})
                raise
            finally:
                duration = time.monotonic() - start
                if SEARCH_REQUEST_DURATION:
                    SEARCH_REQUEST_DURATION.record(duration, attributes={"provider": "brave", "engine": tool_name})
        return wrapper
    return decorator


def register_tools(mcp, get_pool) -> None:
    web.register(mcp, get_pool, metrics=_metrics_wrapper)
