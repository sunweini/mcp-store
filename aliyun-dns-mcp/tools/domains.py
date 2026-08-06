"""域名工具：list_domains（按账户查域名列表）。"""
import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tools import ToolContext, map_aliyun_error
from aliyun_client import AlidnsError

logger = structlog.get_logger()


async def list_domains(account_id: str, *, ctx: ToolContext | None = None) -> dict:
    """查询指定阿里云账户的域名列表（DescribeDomains）。

    返回 [{domain_name, dns_servers, record_count}]，取前 100 条。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized",
                "request_id": None}
    await ctx.checker.require(account_id, "read")
    try:
        client = ctx.clients.get(account_id)
        domains = await client.describe_domains(page_size=100, page_num=1)
    except AlidnsError as e:
        # 凭证失效（I3）等分类联动集中在统一入口（tools.map_aliyun_error）
        return await map_aliyun_error(e, account_id, ctx)
    return {"status": "ok", "data": domains, "count": len(domains)}


def register(mcp: FastMCP, get_ctx, metrics=None) -> None:
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_list_domains(account_id: str) -> dict:
        return await list_domains(account_id, ctx=get_ctx())

    _mcp_list_domains.__doc__ = list_domains.__doc__
    mcp.tool(
        name="list_domains",
        description=list_domains.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("list_domains")(_mcp_list_domains))
