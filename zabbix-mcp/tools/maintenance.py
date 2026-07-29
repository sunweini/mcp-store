"""Maintenance period management tools.

Create, list, and delete Zabbix maintenance periods.
Write tools (create/delete) annotated destructiveHint=True — AI should
confirm parameters (host, time range) with user before executing.
Read tool (list) annotated readOnlyHint — safe for AI to auto-execute.

Design note: functions are defined at module level (not as closures inside
register()) so tests can import and call them directly with a mock zabbix
client. register() creates thin MCP wrappers that inject the real client.
"""
from datetime import datetime

import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from zabbix_client import ZabbixClient, ZabbixAPIError, ZabbixConnectionError

logger = structlog.get_logger()


def _parse_time(time_str: str) -> int:
    """Parse ISO 8601 datetime string to Unix timestamp.

    Raises ValueError if format is invalid — callers convert to user-facing
    error response rather than letting it propagate to the MCP layer.
    """
    dt = datetime.fromisoformat(time_str)
    return int(dt.timestamp())


# ── Module-level tool implementations ──────────────────────────────────────────
# These take zabbix as an explicit keyword-only parameter so tests can inject
# a mock client. register() wraps them to inject the real client from app state.


async def create_maintenance(
    name: str,
    host_names: list[str] | None = None,
    host_group_names: list[str] | None = None,
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    recurring: str | None = None,
    recurring_days: list[int] | None = None,
    recurring_start: str | None = None,
    recurring_end: str | None = None,
    *,
    zabbix: ZabbixClient | None = None,
) -> dict:
    """创建维护期。
    ⚠️ 写操作 — 执行前必须向用户确认参数（主机、时间范围）后再调用。

    host_names 和 host_group_names 至少传一个。
    支持一次性维护 + 周期性维护（如每周二凌晨 2-6 点）。
    start_time / end_time: ISO 8601 格式（如 2026-07-30T02:00:00）。
    recurring: daily / weekly / monthly（可选）。
    """
    # Validate before touching the network — at least one target is required
    if not host_names and not host_group_names:
        return {
            "status": "error",
            "message": "必须提供 host_names 或 host_group_names 中的至少一个",
        }

    if zabbix is None:
        return {"status": "error", "message": "zabbix client not initialized"}

    # Parse times early — fail fast on bad input before any API calls
    try:
        active_since = _parse_time(start_time)
        active_till = _parse_time(end_time)
    except ValueError as e:
        return {"status": "error", "message": f"时间格式错误: {e}"}

    # Resolve host names → hostids via host.get.
    # Zabbix maintenance.create requires hostids, not names.
    host_ids = []
    if host_names:
        try:
            hosts = await zabbix.call(
                "host.get",
                {"filter": {"host": host_names}, "output": ["hostid", "name"]},
            )
        except (ZabbixAPIError, ZabbixConnectionError) as e:
            logger.error(
                "create_maintenance_host_lookup_failed",
                service="zabbix-mcp",
                error=str(e),
            )
            return {"status": "error", "message": f"查询主机失败: {e}"}

        found_names = {h["name"] for h in hosts}
        missing = set(host_names) - found_names
        if missing:
            return {
                "status": "error",
                "message": f"主机不存在: {', '.join(missing)}",
            }
        host_ids = [h["hostid"] for h in hosts]

    # Resolve host group names → groupids via hostgroup.get
    group_ids = []
    if host_group_names:
        try:
            groups = await zabbix.call(
                "hostgroup.get",
                {"filter": {"name": host_group_names}, "output": ["groupid", "name"]},
            )
        except (ZabbixAPIError, ZabbixConnectionError) as e:
            logger.error(
                "create_maintenance_group_lookup_failed",
                service="zabbix-mcp",
                error=str(e),
            )
            return {"status": "error", "message": f"查询主机组失败: {e}"}

        found_names = {g["name"] for g in groups}
        missing = set(host_group_names) - found_names
        if missing:
            return {
                "status": "error",
                "message": f"主机组不存在: {', '.join(missing)}",
            }
        group_ids = [g["groupid"] for g in groups]

    # Build maintenance.create params.
    # timeperiods required by Zabbix API — use one-time (type=0) by default.
    params = {
        "name": name,
        "active_since": str(active_since),
        "active_till": str(active_till),
        "description": description,
        "hostids": host_ids,
        "groupids": group_ids,
        "timeperiods": [
            {
                "timeperiod_type": 0,  # one-time maintenance period
                "start_date": active_since,
                "period": active_till - active_since,
            }
        ],
    }

    # NOTE: Recurring maintenance uses different timeperiod_type values
    # (daily=2, weekly=3, monthly=4). Support deferred until needed —
    # most maintenance windows in practice are one-time.
    if recurring:
        logger.warning(
            "recurring_maintenance_not_yet_supported",
            service="zabbix-mcp",
            recurring=recurring,
        )

    try:
        result = await zabbix.call("maintenance.create", params)
    except (ZabbixAPIError, ZabbixConnectionError) as e:
        logger.error(
            "create_maintenance_failed",
            service="zabbix-mcp",
            error=str(e),
            maintenance_name=name,
        )
        return {"status": "error", "message": f"创建维护期失败: {e}"}

    ids = result.get("maintenanceids", [])
    return {
        "status": "ok",
        "data": {"maintenance_id": ids[0] if ids else None},
    }


async def list_maintenances(
    active_only: bool = True,
    *,
    zabbix: ZabbixClient | None = None,
) -> dict:
    """查看维护期列表。返回名称、关联主机、时间范围、状态。"""
    if zabbix is None:
        return {"status": "error", "message": "zabbix client not initialized"}

    params = {
        "output": "extend",
        "selectHosts": ["name"],
        "selectGroups": ["name"],
        "selectTimeperiods": "extend",
    }

    try:
        maintenances = await zabbix.call("maintenance.get", params)
    except (ZabbixAPIError, ZabbixConnectionError) as e:
        logger.error(
            "list_maintenances_failed",
            service="zabbix-mcp",
            error=str(e),
        )
        return {"status": "error", "message": str(e)}

    data = []
    for m in maintenances:
        # Zabbix nests host/group objects; flatten to name lists for LLM readability
        hosts = [h["name"] for h in m.get("hosts", [])]
        groups = [g["name"] for g in m.get("groups", [])]
        data.append({
            "maintenance_id": m.get("maintenanceid"),
            "name": m.get("name"),
            "description": m.get("description", ""),
            "hosts": hosts,
            "host_groups": groups,
            "active_since": m.get("active_since"),
            "active_till": m.get("active_till"),
        })

    return {"status": "ok", "data": data, "count": len(data)}


async def delete_maintenance(
    maintenance_id: str,
    *,
    zabbix: ZabbixClient | None = None,
) -> dict:
    """删除/结束维护期。
    ⚠️ 写操作 — 执行前必须向用户确认后再调用。
    """
    if zabbix is None:
        return {"status": "error", "message": "zabbix client not initialized"}

    try:
        await zabbix.call("maintenance.delete", [maintenance_id])
    except (ZabbixAPIError, ZabbixConnectionError) as e:
        logger.error(
            "delete_maintenance_failed",
            service="zabbix-mcp",
            error=str(e),
            maintenance_id=maintenance_id,
        )
        return {"status": "error", "message": f"删除维护期失败: {e}"}

    return {"status": "ok", "data": {"maintenance_id": maintenance_id}}


# ── MCP registration ───────────────────────────────────────────────────────────


def register(mcp: FastMCP, get_zabbix, metrics=None) -> None:
    """Register maintenance tools on the FastMCP server.

    get_zabbix: callable returning the ZabbixClient from app state.
    Uses a closure to defer client lookup until tool invocation time —
    the client isn't available at import/registration time in stateless mode.

    metrics: optional _metrics_wrapper factory from tools/__init__.py.
    """
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_create_maintenance(
        name: str,
        host_names: list[str] | None = None,
        host_group_names: list[str] | None = None,
        start_time: str = "",
        end_time: str = "",
        description: str = "",
        recurring: str | None = None,
        recurring_days: list[int] | None = None,
        recurring_start: str | None = None,
        recurring_end: str | None = None,
    ) -> dict:
        return await create_maintenance(
            name=name,
            host_names=host_names,
            host_group_names=host_group_names,
            start_time=start_time,
            end_time=end_time,
            description=description,
            recurring=recurring,
            recurring_days=recurring_days,
            recurring_start=recurring_start,
            recurring_end=recurring_end,
            zabbix=get_zabbix(),
        )

    _mcp_create_maintenance.__doc__ = create_maintenance.__doc__

    mcp.tool(
        name="create_maintenance",
        description=create_maintenance.__doc__,
        annotations=ToolAnnotations(destructiveHint=True),
    )(_wrap("create_maintenance")(_mcp_create_maintenance))

    async def _mcp_list_maintenances(active_only: bool = True) -> dict:
        return await list_maintenances(active_only=active_only, zabbix=get_zabbix())

    _mcp_list_maintenances.__doc__ = list_maintenances.__doc__

    mcp.tool(
        name="list_maintenances",
        description=list_maintenances.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("list_maintenances")(_mcp_list_maintenances))

    async def _mcp_delete_maintenance(maintenance_id: str) -> dict:
        return await delete_maintenance(maintenance_id=maintenance_id, zabbix=get_zabbix())

    _mcp_delete_maintenance.__doc__ = delete_maintenance.__doc__

    mcp.tool(
        name="delete_maintenance",
        description=delete_maintenance.__doc__,
        annotations=ToolAnnotations(destructiveHint=True),
    )(_wrap("delete_maintenance")(_mcp_delete_maintenance))
