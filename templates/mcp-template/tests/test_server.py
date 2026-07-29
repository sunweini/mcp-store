"""Tests for {{MCP_NAME}}."""
import pytest
from fastmcp import Client


@pytest.fixture
async def client():
    """Create a test client connected to the server."""
    async with Client("server.py") as c:
        yield c


@pytest.mark.asyncio
async def test_list_tools(client):
    tools = await client.list_tools()
    assert len(tools) >= 1
    names = {t.name for t in tools}
    assert "hello" in names


@pytest.mark.asyncio
async def test_hello_tool(client):
    result = await client.call_tool("hello", {"name": "World"})
    assert "World" in str(result)


@pytest.mark.asyncio
async def test_version_resource(client):
    resources = await client.list_resources()
    assert len(resources) >= 1
