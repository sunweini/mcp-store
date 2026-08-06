"""注册冒烟：register_tools 注册 6 个工具，含读写标注。"""
import pytest
from fastmcp import FastMCP

from tools import register_tools, ToolContext


@pytest.mark.asyncio
async def test_register_tools_six_tools():
    # NOTE: FastMCP v4.0.0b1 无 mcp.get_tools()（同步），实测 list_tools()
    # 是 async 协程且返回 FunctionTool list——以实测为准，与 brief 断言等价
    mcp = FastMCP("Test")
    ctx = ToolContext(checker=object(), clients=object())
    register_tools(mcp, lambda: ctx, metrics=None)
    tools = {t.name: t for t in await mcp.list_tools()}
    assert set(tools) == {"list_accounts", "list_domains", "list_records",
                          "add_record", "update_record", "delete_record"}
    assert tools["add_record"].annotations.destructive_hint is True
    assert tools["list_domains"].annotations.read_only_hint is True
    assert "⚠️ 写操作" in (tools["add_record"].description or "")
