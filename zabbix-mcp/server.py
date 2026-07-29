"""Zabbix MCP Server — entry point.

Provides Zabbix monitoring tools via MCP 2026-07-28 stateless protocol.
Uses API Token auth (no user.login session), compatible with stateless deployments.
"""
import os
from contextlib import asynccontextmanager

from fastmcp import FastMCP

# NOTE: env vars required — no defaults for Zabbix connection
ZABBIX_URL = os.environ.get("ZABBIX_URL", "")
ZABBIX_TOKEN = os.environ.get("ZABBIX_TOKEN", "")
ZABBIX_TIMEOUT = float(os.environ.get("ZABBIX_TIMEOUT", "30"))
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))

mcp = FastMCP(
    "Zabbix MCP",
    instructions=(
        "Provides tools for Zabbix monitoring: alert patrol, "
        "maintenance management, and alert acknowledgment. "
        "Start with list_active_problems() or problem_summary() for current state."
    ),
)


@asynccontextmanager
async def lifespan(app):
    # NOTE: ZabbixClient initialized per-process via lifespan,
    # not per-request, because httpx connection pool is expensive to create
    from zabbix_client import ZabbixClient

    if not ZABBIX_URL or not ZABBIX_TOKEN:
        raise RuntimeError(
            "ZABBIX_URL and ZABBIX_TOKEN environment variables are required"
        )
    app.state.zabbix = ZabbixClient(
        url=ZABBIX_URL, token=ZABBIX_TOKEN, timeout=ZABBIX_TIMEOUT
    )
    yield
    await app.state.zabbix.close()


# Tools will be registered here by tools/__init__.py
# from tools import register_tools
# register_tools(mcp)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        host=MCP_HOST,
        port=MCP_PORT,
    )
