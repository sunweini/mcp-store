"""工具注册模块：模块级函数 + 显式具名包装（FastMCP v4 拒绝 *args 包装）。

工具函数定义在 tools/*.py 模块级（可独立测试），register() 只做薄包装：
注入真实 ctx（checker/clients），复制 docstring 作为 tool 描述。
"""
import functools
import time

import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from dataclasses import dataclass

logger = structlog.get_logger()

try:
    from telemetry import REQUESTS_TOTAL, REQUEST_DURATION, ERRORS_TOTAL, IN_FLIGHT_REQUESTS
except ImportError:
    # telemetry 未就绪（Task 7 前）时指标为 None，record 前 guard
    REQUESTS_TOTAL = REQUEST_DURATION = ERRORS_TOTAL = IN_FLIGHT_REQUESTS = None


@dataclass
class ToolContext:
    """工具依赖上下文：checker（账户级鉴权）+ clients（Alidns 客户端工厂）。"""
    checker: object
    clients: object


def _metrics_wrapper(tool_name: str):
    """记录 tool 级 Prometheus 指标（zabbix 模式）。"""
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
                if isinstance(result, dict) and result.get("status") == "error" and ERRORS_TOTAL:
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


def register_tools(mcp: FastMCP, get_ctx, metrics=None) -> None:
    """注册全部工具。get_ctx: callable 返回 ToolContext（server.py 注入）。"""
    from tools import accounts, domains, records
    accounts.register(mcp, get_ctx, metrics=metrics or _metrics_wrapper)
    domains.register(mcp, get_ctx, metrics=metrics or _metrics_wrapper)
    records.register(mcp, get_ctx, metrics=metrics or _metrics_wrapper)
