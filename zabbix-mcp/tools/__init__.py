"""Tool registration module.

Each sub-module exports a register(mcp, get_zabbix) function that attaches tools.
This keeps tool definitions isolated and testable independently.

get_zabbix: callable returning the ZabbixClient from app state.
Deferred lookup (closure) because the client is initialized in the lifespan,
not at import time.
"""
from tools import problems, maintenance, events


def register_tools(mcp, get_zabbix) -> None:
    """Register all Zabbix tools on the FastMCP server instance."""
    problems.register(mcp, get_zabbix)
    maintenance.register(mcp, get_zabbix)
    events.register(mcp, get_zabbix)
