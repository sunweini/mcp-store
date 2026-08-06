"""{{MCP_NAME}} — MCP Server

Protocol: MCP 2026-07-28 (stateless HTTP)
Framework: FastMCP 4.0.0b1
Gateway-ready: annotations 读写分离 / tool 描述 / MCP ping 探活 / structlog + OTel
"""
import os

import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

# ── Configuration ────────────────────────────────────────────────
# NOTE: MCP_PORT 必须用根 CLAUDE.md 端口表登记的最小未用端口（9050-9500），
# 不要默认 8000——容器内端口规范统一，登记后再开发。
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "9054"))

logger = structlog.get_logger()

# NOTE: instructions 让 client/AI 理解本 MCP 能力，务必写清楚。
mcp = FastMCP(
    "{{MCP_NAME}}",
    instructions="{{一句话描述本 MCP 的能力与使用方式}}",
)


# ── 读操作 Tool ──────────────────────────────────────────────────
# NOTE: readOnlyHint=True → Gateway 判定为 read，token 有 read 权限即可调用。
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_items(query: str = "", limit: int = 50) -> list[dict]:
    """查询条目列表（示例读操作，替换为真实逻辑）。

    返回匹配 query 的条目，按时间降序。
    """
    logger.info("list_items", service="{{mcp-name}}", query=query, limit=limit)
    return [{"id": i, "name": f"item-{i}"} for i in range(limit)]


# ── 写操作 Tool ──────────────────────────────────────────────────
# NOTE: destructiveHint=True → Gateway 判定为 write，需 token 有 write 权限。
# docstring 含「⚠️ 写操作」标记，AI 读到后走用户确认流程。
@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
def create_item(name: str) -> dict:
    """创建条目（示例写操作，替换为真实逻辑）。

    ⚠️ 写操作 — 执行前必须向用户确认参数后再调用。
    """
    logger.info("create_item", service="{{mcp-name}}", name=name)
    return {"id": "new-id", "name": name, "created": True}


# ── Resources ────────────────────────────────────────────────────
@mcp.resource("info://version")
def get_version() -> str:
    """Return server version."""
    return "0.1.0"


# ── Prompts ──────────────────────────────────────────────────────
@mcp.prompt()
def help_prompt() -> str:
    """Show available operations."""
    return "Available tools: list_items (read), create_item (write)."


if __name__ == "__main__":
    # NOTE: stateless_http=True 是接入 Gateway 的硬性要求。
    # MCP 标准 ping 由 FastMCP 原生提供，Gateway 据此探活，无需额外实现。
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        host=HOST,
        port=PORT,
    )
