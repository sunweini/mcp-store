"""
{{MCP_NAME}} — MCP Server

Protocol: 2026-07-28 (stateless HTTP)
Framework: FastMCP v4
"""
import os

from fastmcp import FastMCP

# ── Configuration ────────────────────────────────────────────────
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))

mcp = FastMCP("{{MCP_NAME}}")


# ── Tools ────────────────────────────────────────────────────────
@mcp.tool()
def hello(name: str) -> str:
    """Say hello. Replace with real tools."""
    return f"Hello, {name}!"


# ── Resources ────────────────────────────────────────────────────
@mcp.resource("info://version")
def get_version() -> str:
    """Return server version."""
    return "0.1.0"


# ── Prompts ──────────────────────────────────────────────────────
@mcp.prompt()
def help_prompt() -> str:
    """Show available operations."""
    return "Available tools: hello. Call hello(name) to get started."


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        host=HOST,
        port=PORT,
    )
