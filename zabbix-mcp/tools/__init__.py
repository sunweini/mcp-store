"""Tool registration module.

Each sub-module exports a register(mcp) function that attaches tools.
This keeps tool definitions isolated and testable independently.
"""


def register_tools(mcp) -> None:
    """Register all Zabbix tools on the FastMCP server instance."""
    # Will import and call register() from each tool module
    pass
