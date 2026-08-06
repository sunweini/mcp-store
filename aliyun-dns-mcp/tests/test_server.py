"""Tests for Aliyun DNS MCP."""
import pytest
from fastmcp import Client

from server import mcp


@pytest.fixture
async def client():
    """Create an in-memory client connected to the server.

    NOTE: 模板原版用 Client("server.py")（stdio 子进程），但 server.py 只跑
    streamable-http transport，子进程永不就绪导致测试挂死。改用 in-memory
    Client(mcp)（知识库 41-testing.md 推荐写法）。
    """
    async with Client(mcp) as c:
        yield c


@pytest.mark.asyncio
async def test_list_tools(client):
    tools = await client.list_tools()
    assert len(tools) >= 1
    names = {t.name for t in tools}
    assert "list_items" in names


@pytest.mark.asyncio
async def test_list_items_tool(client):
    result = await client.call_tool("list_items", {"query": "demo"})
    assert "item" in str(result)


@pytest.mark.asyncio
async def test_version_resource(client):
    resources = await client.list_resources()
    assert len(resources) >= 1
