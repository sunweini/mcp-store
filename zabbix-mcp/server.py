"""Zabbix MCP Server — entry point.

Provides Zabbix monitoring tools via MCP 2026-07-28 stateless protocol.
Uses API Token auth (no user.login session), compatible with stateless deployments.

Observability:
- Structured logging via structlog with OTel trace context injection
- OpenTelemetry traces for all Zabbix API calls (see zabbix_client.py)
- Prometheus metrics at /metrics (default port 9464)
- Env: OTEL_EXPORTER_OTLP_ENDPOINT, LOG_FORMAT=json|console
"""
import os

import structlog
from fastmcp import FastMCP
from opentelemetry import trace

# NOTE: env vars required — no defaults for Zabbix connection
ZABBIX_URL = os.environ.get("ZABBIX_URL", "")
ZABBIX_TOKEN = os.environ.get("ZABBIX_TOKEN", "")
ZABBIX_TIMEOUT = float(os.environ.get("ZABBIX_TIMEOUT", "30"))
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "9053"))
LOG_FORMAT = os.environ.get("LOG_FORMAT", "console")  # "console" or "json"


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

    # OBS-CORE-001: delegate to shared helper so LOG_FILE env also works here
    from logging_config import configure_logging
    configure_logging([
        structlog.contextvars.merge_contextvars,
        add_trace_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer() if LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(),
    ])


# Process-level ZabbixClient, initialized at module load time.
# NOTE: In stateless mode, FastMCP v4 lifespan doesn't reliably trigger
# on every request. Module-level init is simpler and works for single-process
# deployments. Multi-process deployments should use shared storage.
_zabbix_client = None


def _init_zabbix_client():
    """Initialize ZabbixClient from env vars. Called at module load."""
    global _zabbix_client

    if not ZABBIX_URL or not ZABBIX_TOKEN:
        raise RuntimeError(
            "ZABBIX_URL and ZABBIX_TOKEN environment variables are required"
        )

    from zabbix_client import ZabbixClient
    _zabbix_client = ZabbixClient(
        url=ZABBIX_URL, token=ZABBIX_TOKEN, timeout=ZABBIX_TIMEOUT
    )

    structlog.get_logger().info(
        "zabbix_client_initialized",
        service="zabbix-mcp",
        zabbix_url=ZABBIX_URL,
    )


def _get_zabbix():
    """Return the process-level ZabbixClient.

    Raises RuntimeError if env vars are missing — check ZABBIX_URL/ZABBIX_TOKEN.
    """
    if _zabbix_client is None:
        raise RuntimeError("ZabbixClient not initialized — check ZABBIX_URL/ZABBIX_TOKEN")
    return _zabbix_client


# Configure logging before any log calls
_configure_logging()

# Initialize OTel traces + metrics (no-op if SDK not installed)
from telemetry import init_telemetry
init_telemetry()

# Initialize ZabbixClient at module load (stateless mode doesn't trigger lifespan)
_init_zabbix_client()

mcp = FastMCP(
    "Zabbix MCP",
    instructions=(
        "Provides tools for Zabbix monitoring: alert patrol, "
        "maintenance management, and alert acknowledgment. "
        "Start with list_active_problems() or problem_summary() for current state. "
        "Write tools (create/delete maintenance, acknowledge) require user confirmation."
    ),
)


# Register all tools — tools access the ZabbixClient via _get_zabbix closure.
from tools import register_tools

register_tools(mcp, _get_zabbix)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        host=MCP_HOST,
        port=MCP_PORT,
    )
