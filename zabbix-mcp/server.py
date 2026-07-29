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

# NOTE: process-level ZabbixClient reference, initialized in lifespan.
# Tools receive a callable (_get_zabbix) that reads this variable,
# avoiding the need to thread app.state through the tool registration layer.
_zabbix_client = None


def _get_zabbix():
    """Return the process-level ZabbixClient.

    Raises RuntimeError if called before lifespan has initialized the client —
    this indicates a startup ordering bug, not a transient error.
    """
    if _zabbix_client is None:
        raise RuntimeError("ZabbixClient not initialized — lifespan has not run yet")
    return _zabbix_client


@asynccontextmanager
async def lifespan(app):
    # NOTE: ZabbixClient initialized per-process via lifespan,
    # not per-request, because httpx connection pool is expensive to create
    from zabbix_client import ZabbixClient

    global _zabbix_client

    if not ZABBIX_URL or not ZABBIX_TOKEN:
        raise RuntimeError(
            "ZABBIX_URL and ZABBIX_TOKEN environment variables are required"
        )
    _zabbix_client = ZabbixClient(
        url=ZABBIX_URL, token=ZABBIX_TOKEN, timeout=ZABBIX_TIMEOUT
    )
    yield
    await _zabbix_client.close()
    _zabbix_client = None


# Tool registration — deferred via _get_zabbix closure so tools can access
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
