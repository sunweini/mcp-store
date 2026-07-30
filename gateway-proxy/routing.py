"""Namespace prefix routing + tool mode registry.

FastMCP mount(namespace=name) exposes tools as {server}_{tool}. This module
splits that prefix to find the target server, and looks up whether the tool
is a read or write operation (from annotations.destructiveHint at introspect time).
"""
# TOOL_REGISTRY: {server: {tool_name: mode}}
# NOTE: module-level dict mutated by registry.mount_server on hot-reload.
TOOL_REGISTRY: dict[str, dict[str, str]] = {}


class UnknownServerError(Exception):
    """The namespace prefix does not match any registered server."""


def split_prefix(mcp_name: str) -> tuple[str, str]:
    """Split '{server}_{tool}' into (server, tool).

    Splits on the FIRST underscore. Server names are [a-z0-9-] (no underscores,
    enforced at registration), so the first _ is always the namespace separator.
    Raises ValueError if there is no underscore (no namespace prefix).
    """
    if "_" not in mcp_name:
        raise ValueError(f"no namespace prefix in tool name: {mcp_name}")
    server, tool = mcp_name.split("_", 1)
    return server, tool


def register_tools(server: str, tools: list[dict]) -> None:
    """Record each tool's mode (read/write) for a server.

    Called by registry when mounting/refreshing a server.
    Overwrites previous entries for this server (handles update).
    """
    TOOL_REGISTRY[server] = {t["name"]: t["mode"] for t in tools}


def clear_tools(server: str) -> None:
    """Remove a server's tools from the registry (on unmount)."""
    TOOL_REGISTRY.pop(server, None)


def get_tool_mode(server: str, tool: str) -> str:
    """Return 'read' or 'write' for a server's tool. Defaults to 'read'."""
    return TOOL_REGISTRY.get(server, {}).get(tool, "read")


def resolve_target(mcp_name: str) -> tuple[str, str, str]:
    """Resolve a namespaced tool name to (server, tool, mode).

    Raises UnknownServerError if the server prefix is not registered.
    """
    server, tool = split_prefix(mcp_name)
    if server not in TOOL_REGISTRY:
        raise UnknownServerError(f"server '{server}' not registered")
    return server, tool, get_tool_mode(server, tool)
