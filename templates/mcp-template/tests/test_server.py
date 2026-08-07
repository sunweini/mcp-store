"""Tests for {{MCP_NAME}}.

NOTE: 用 Client(mcp) in-memory 而非 Client("server.py") stdio——模板 server.py
的 __main__ 只跑 streamable-http transport，stdio 子进程永不就绪会挂死
（实测 180s 超时）。in-memory 是 FastMCP 官方推荐的测试方式（41-testing.md）。
"""
import pytest
from fastmcp import Client

import server


@pytest.fixture
async def client():
    """Create an in-memory test client connected to the server."""
    async with Client(server.mcp) as c:
        yield c


@pytest.mark.asyncio
async def test_list_tools(client):
    tools = await client.list_tools()
    assert len(tools) >= 1
    names = {t.name for t in tools}
    assert "list_items" in names


@pytest.mark.asyncio
async def test_list_items_tool(client):
    result = await client.call_tool("list_items", {"limit": 3})
    assert "item" in str(result)


@pytest.mark.asyncio
async def test_version_resource(client):
    resources = await client.list_resources()
    assert len(resources) >= 1
