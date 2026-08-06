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
    # 模板示例 tool 已删（Task 7），6 个真实工具由 register_tools 注入
    assert names == {"list_accounts", "list_domains", "list_records",
                     "add_record", "update_record", "delete_record"}


@pytest.mark.asyncio
async def test_tools_annotations(client):
    """读写标注回归：写工具 destructive、读工具 read-only。"""
    tools = {t.name: t for t in await client.list_tools()}
    assert tools["add_record"].annotations.destructive_hint is True
    assert tools["list_domains"].annotations.read_only_hint is True
    assert "⚠️ 写操作" in (tools["add_record"].description or "")
