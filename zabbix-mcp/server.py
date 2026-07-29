"""Zabbix MCP Server — entry point.

Provides Zabbix monitoring tools via MCP 2026-07-28 stateless protocol.
Uses API Token auth (no user.login session), compatible with stateless deployments.

Observability:
- Structured logging via structlog with OTel trace context injection
- OpenTelemetry traces for all Zabbix API calls (see zabbix_client.py)
"""
import os
from contextlib import asynccontextmanager

import structlog
from fastmcp import FastMCP
from opentelemetry import trace

# NOTE: env vars required — no defaults for Zabbix connection
ZABBIX_URL = os.environ.get("ZABBIX_URL", "")
ZABBIX_TOKEN = os.environ.get("ZABBIX_TOKEN", "")
ZABBIX_TIMEOUT = float(os.environ.get("ZABBIX_TIMEOUT", "30"))
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))


def _configure_logging() -> None:
    """Configure structlog with OTel trace context injection.

    OBS-CORR-001: 每条日志自动注入 trace_id + span_id。
    OBS-CORE-001: 所有日志结构化 key=value。
    """

    def add_trace_context(logger, method_name, event_dict):
        """从当前 OTel span 提取 trace_id/span_id 注入日志。"""
        span = trace.get_current_span()
        sc = span.get_span_context()
        if sc and sc.is_valid:
            event_dict["trace_id"] = format(sc.trace_id, "032x")
            event_dict["span_id"] = format(sc.span_id, "016x")
        return event_dict

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_trace_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
            # NOTE: 生产环境换为 structlog.processors.JSONRenderer()
        ],
    )


# Process-level ZabbixClient, initialized during lifespan
_zabbix_client = None


def _get_zabbix():
    """Return the process-level ZabbixClient.

    Raises RuntimeError if called before lifespan has initialized the client —
    this indicates a startup ordering bug, not a transient error.
    """
    if _zabbix_client is None:
        raise RuntimeError("ZabbixClient not initialized — check ZABBIX_URL/ZABBIX_TOKEN")
    return _zabbix_client


# Configure logging before any log calls
_configure_logging()

mcp = FastMCP(
    "Zabbix MCP",
    instructions=(
        "Provides tools for Zabbix monitoring: alert patrol, "
        "maintenance management, and alert acknowledgment. "
        "Start with list_active_problems() or problem_summary() for current state. "
        "Write tools (create/delete maintenance, acknowledge) require user confirmation."
    ),
)


@asynccontextmanager
async def lifespan(app):
    """Initialize ZabbixClient on startup, close on shutdown."""
    global _zabbix_client

    from zabbix_client import ZabbixClient

    if not ZABBIX_URL or not ZABBIX_TOKEN:
        raise RuntimeError(
            "ZABBIX_URL and ZABBIX_TOKEN environment variables are required"
        )

    _zabbix_client = ZabbixClient(
        url=ZABBIX_URL, token=ZABBIX_TOKEN, timeout=ZABBIX_TIMEOUT
    )

    structlog.get_logger().info(
        "zabbix_client_initialized",
        service="zabbix-mcp",
        zabbix_url=ZABBIX_URL,
    )

    yield

    await _zabbix_client.close()
    _zabbix_client = None
    structlog.get_logger().info("zabbix_client_closed", service="zabbix-mcp")


# Register all tools — deferred via _get_zabbix closure so tools access
# the client initialized in lifespan without importing server state directly.
from tools import register_tools

register_tools(mcp, _get_zabbix)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        host=MCP_HOST,
        port=MCP_PORT,
    )
