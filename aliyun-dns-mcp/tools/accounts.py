"""账户工具：list_accounts（当前 token 可访问的托管账户）。"""
import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tools import ToolContext

logger = structlog.get_logger()


async def list_accounts(*, ctx: ToolContext | None = None) -> dict:
    """列出当前 token 可访问的阿里云账户及其读写权限。

    返回 [{account_id, description, read, write}]，只含已托管且启用的账户；
    不暴露 AccessKey 等凭证信息。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized",
                "request_id": None}
    accounts = await ctx.checker.allowed_accounts()
    return {"status": "ok", "data": accounts, "count": len(accounts)}


def register(mcp: FastMCP, get_ctx, metrics=None) -> None:
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_list_accounts() -> dict:
        return await list_accounts(ctx=get_ctx())

    _mcp_list_accounts.__doc__ = list_accounts.__doc__
    mcp.tool(
        name="list_accounts",
        description=list_accounts.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("list_accounts")(_mcp_list_accounts))
