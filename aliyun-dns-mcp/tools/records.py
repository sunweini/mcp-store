"""解析记录工具：list/add/update/delete（写操作走 ⚠️ 用户确认流程）。"""
import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from tools import ToolContext, map_aliyun_error
from aliyun_client import AlidnsError

logger = structlog.get_logger()


async def list_records(account_id: str, domain_name: str, *, ctx: ToolContext | None = None) -> dict:
    """查询指定账户、主域名的 DNS 解析记录列表（DescribeDomainRecords）。

    返回 [{record_id, rr, type, value, ttl, priority, status}]，取前 100 条。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized",
                "request_id": None}
    await ctx.checker.require(account_id, "read")
    try:
        client = ctx.clients.get(account_id)
        records = await client.describe_domain_records(domain_name, page_size=100, page_num=1)
    except AlidnsError as e:
        return await map_aliyun_error(e, account_id, ctx)
    return {"status": "ok", "data": records, "count": len(records)}


async def add_record(account_id: str, domain_name: str, rr: str, type: str, value: str,
                     ttl: int = 600, priority: int | None = None, *,
                     ctx: ToolContext | None = None) -> dict:
    """新增 DNS 解析记录（AddDomainRecord）。

    ⚠️ 写操作 — 执行前必须向用户确认参数后再调用。

    type 支持 A/AAAA/CNAME/TXT/MX/NS/SRV/CAA 等阿里云全部类型；
    priority 仅 MX/SRV 需要。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized",
                "request_id": None}
    await ctx.checker.require(account_id, "write")
    try:
        client = ctx.clients.get(account_id)
        record_id = await client.add_domain_record(
            domain_name, rr, type, value, ttl=ttl, priority=priority)
    except AlidnsError as e:
        return await map_aliyun_error(e, account_id, ctx)
    logger.info("record_added", service="aliyun-dns-mcp", account_id=account_id,
                domain_name=domain_name, rr=rr, type=type)
    return {"status": "ok", "data": {"record_id": record_id}}


async def update_record(account_id: str, record_id: str,
                        rr: str | None = None, type: str | None = None,
                        value: str | None = None, ttl: int | None = None,
                        priority: int | None = None, *,
                        ctx: ToolContext | None = None) -> dict:
    """修改 DNS 解析记录（UpdateDomainRecord）。

    ⚠️ 写操作 — 执行前必须向用户确认参数后再调用。

    至少传一个更新字段；未传的字段保持不变。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized",
                "request_id": None}
    if all(v is None for v in (rr, type, value, ttl, priority)):
        return {"status": "error", "error_type": "invalid_params",
                "message": "至少提供一个更新字段 (rr/type/value/ttl/priority)"}
    await ctx.checker.require(account_id, "write")
    try:
        client = ctx.clients.get(account_id)
        await client.update_domain_record(record_id, rr=rr, type=type, value=value,
                                          ttl=ttl, priority=priority)
    except AlidnsError as e:
        return await map_aliyun_error(e, account_id, ctx)
    logger.info("record_updated", service="aliyun-dns-mcp", account_id=account_id, record_id=record_id)
    return {"status": "ok", "data": {"record_id": record_id}}


async def delete_record(account_id: str, record_id: str, *, ctx: ToolContext | None = None) -> dict:
    """删除 DNS 解析记录（DeleteDomainRecord）。

    ⚠️ 写操作 — 删除不可撤销，执行前必须向用户确认。
    """
    if ctx is None:
        return {"status": "error", "error_type": "internal", "message": "context not initialized",
                "request_id": None}
    await ctx.checker.require(account_id, "write")
    try:
        client = ctx.clients.get(account_id)
        await client.delete_domain_record(record_id)
    except AlidnsError as e:
        return await map_aliyun_error(e, account_id, ctx)
    logger.info("record_deleted", service="aliyun-dns-mcp", account_id=account_id, record_id=record_id)
    return {"status": "ok", "data": {"record_id": record_id}}


def register(mcp: FastMCP, get_ctx, metrics=None) -> None:
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_list_records(account_id: str, domain_name: str) -> dict:
        return await list_records(account_id, domain_name, ctx=get_ctx())

    _mcp_list_records.__doc__ = list_records.__doc__
    mcp.tool(name="list_records", description=list_records.__doc__,
             annotations=ToolAnnotations(readOnlyHint=True))(_wrap("list_records")(_mcp_list_records))

    async def _mcp_add_record(account_id: str, domain_name: str, rr: str, type: str,
                              value: str, ttl: int = 600,
                              priority: int | None = None) -> dict:
        return await add_record(account_id, domain_name, rr, type, value,
                                ttl=ttl, priority=priority, ctx=get_ctx())

    _mcp_add_record.__doc__ = add_record.__doc__
    mcp.tool(name="add_record", description=add_record.__doc__,
             annotations=ToolAnnotations(destructiveHint=True))(_wrap("add_record")(_mcp_add_record))

    async def _mcp_update_record(account_id: str, record_id: str,
                                 rr: str | None = None, type: str | None = None,
                                 value: str | None = None, ttl: int | None = None,
                                 priority: int | None = None) -> dict:
        return await update_record(account_id, record_id, rr=rr, type=type, value=value,
                                   ttl=ttl, priority=priority, ctx=get_ctx())

    _mcp_update_record.__doc__ = update_record.__doc__
    mcp.tool(name="update_record", description=update_record.__doc__,
             annotations=ToolAnnotations(destructiveHint=True))(_wrap("update_record")(_mcp_update_record))

    async def _mcp_delete_record(account_id: str, record_id: str) -> dict:
        return await delete_record(account_id, record_id, ctx=get_ctx())

    _mcp_delete_record.__doc__ = delete_record.__doc__
    mcp.tool(name="delete_record", description=delete_record.__doc__,
             annotations=ToolAnnotations(destructiveHint=True))(_wrap("delete_record")(_mcp_delete_record))
