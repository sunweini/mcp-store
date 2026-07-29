"""Alert acknowledgment tools — single and batch.

Write tools annotated destructiveHint=True — AI should confirm before executing.
Read tool (list_unacknowledged) annotated readOnlyHint — safe for AI to auto-execute.

Design note: functions are defined at module level (not as closures inside
register()) so tests can import and call them directly with a mock zabbix
client. register() creates thin MCP wrappers that inject the real client.
"""
import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from zabbix_client import ZabbixClient, ZabbixAPIError, ZabbixConnectionError
from tools.problems import _resolve_severity, _format_problem, _resolve_trigger_hosts

logger = structlog.get_logger()

# NOTE: Zabbix event.acknowledge action bitmask — values from Zabbix API docs.
# Combined with bitwise OR to express multiple actions in a single call.
_ACK_ACTION = 1       # mark as acknowledged
_MSG_ACTION = 2       # attach message to acknowledgment
_CLOSE_ACTION = 8     # close the problem after acknowledging


# ── Module-level tool implementations ──────────────────────────────────────────
# These take zabbix as an explicit keyword-only parameter so tests can inject
# a mock client. register() wraps them to inject the real client from app state.


async def list_unacknowledged(
    severity: str | None = None,
    limit: int = 50,
    *,
    zabbix: ZabbixClient | None = None,
) -> dict:
    """查询未确认的活跃告警。返回 event_id 供确认使用。

    按时间降序（最新在前）。
    severity 可选值: not_classified, information, warning, average, high, disaster
    """
    # Validate severity before touching the network — fail fast on bad input
    sev_int = _resolve_severity(severity)
    if severity is not None and sev_int is None:
        return {
            "status": "error",
            "message": f"无效的严重级别: '{severity}'",
        }

    if zabbix is None:
        return {"status": "error", "message": "zabbix client not initialized"}

    params = {
        "output": "extend",
        "selectHosts": ["name"],
        "sortfield": "eventid",
        "sortorder": "DESC",
        "recent": True,
        # NOTE: acknowledged=False maps to Zabbix "0" — only unacknowledged events
        "acknowledged": False,
        "limit": limit,
    }
    if sev_int is not None:
        params["severities"] = [sev_int]

    try:
        problems = await zabbix.call("problem.get", params)
    except (ZabbixAPIError, ZabbixConnectionError) as e:
        logger.error(
            "list_unacknowledged_failed",
            service="zabbix-mcp",
            error=str(e),
            severity=severity,
        )
        return {"status": "error", "message": str(e)}

    host_map = await _resolve_trigger_hosts(problems, zabbix)
    data = [_format_problem(p, host_map) for p in problems]
    return {"status": "ok", "data": data, "count": len(data)}


async def acknowledge_event(
    event_id: str,
    message: str = "",
    close: bool = False,
    *,
    zabbix: ZabbixClient | None = None,
) -> dict:
    """确认单条告警。
    ⚠️ 写操作 — 执行前必须向用户确认后再调用。

    message 记录确认原因（如"已计划维护"、"已知问题"）。
    close=True 同时关闭问题。
    """
    if zabbix is None:
        return {"status": "error", "message": "zabbix client not initialized"}

    # Build action bitmask — always include ACK, conditionally add MSG and CLOSE
    action = _ACK_ACTION
    if message:
        action |= _MSG_ACTION
    if close:
        action |= _CLOSE_ACTION

    params = {
        "eventids": [event_id],
        "action": action,
        "message": message,
    }

    try:
        await zabbix.call("event.acknowledge", params)
    except (ZabbixAPIError, ZabbixConnectionError) as e:
        logger.error(
            "acknowledge_event_failed",
            service="zabbix-mcp",
            error=str(e),
            event_id=event_id,
        )
        return {"status": "error", "message": f"确认告警失败: {e}"}

    return {"status": "ok", "data": {"event_id": event_id, "acknowledged": True}}


async def batch_acknowledge(
    event_ids: list[str],
    message: str = "",
    close: bool = False,
    *,
    zabbix: ZabbixClient | None = None,
) -> dict:
    """批量确认多条告警。
    ⚠️ 写操作 — 执行前必须向用户确认后再调用。

    适用于同一 trigger/host 引发的多条关联告警。
    """
    # Guard: empty list is a user error — reject before touching Zabbix
    if not event_ids:
        return {"status": "error", "message": "event_ids is empty (不能为空)"}

    if zabbix is None:
        return {"status": "error", "message": "zabbix client not initialized"}

    action = _ACK_ACTION
    if message:
        action |= _MSG_ACTION
    if close:
        action |= _CLOSE_ACTION

    params = {
        "eventids": event_ids,
        "action": action,
        "message": message,
    }

    try:
        result = await zabbix.call("event.acknowledge", params)
    except (ZabbixAPIError, ZabbixConnectionError) as e:
        logger.error(
            "batch_acknowledge_failed",
            service="zabbix-mcp",
            error=str(e),
            event_count=len(event_ids),
        )
        return {"status": "error", "message": f"批量确认失败: {e}"}

    acked = result.get("eventids", [])
    return {
        "status": "ok",
        "data": {
            "acknowledged_count": len(acked),
            "event_ids": acked,
        },
    }


# ── MCP registration ───────────────────────────────────────────────────────────


def register(mcp: FastMCP, get_zabbix, metrics=None) -> None:
    """Register event tools on the FastMCP server.

    get_zabbix: callable returning the ZabbixClient from app state.
    Uses a closure to defer client lookup until tool invocation time —
    the client isn't available at import/registration time in stateless mode.

    metrics: optional _metrics_wrapper factory from tools/__init__.py.
    """
    _wrap = metrics or (lambda name: lambda f: f)

    async def _mcp_list_unacknowledged(
        severity: str | None = None,
        limit: int = 50,
    ) -> dict:
        return await list_unacknowledged(
            severity=severity,
            limit=limit,
            zabbix=get_zabbix(),
        )

    _mcp_list_unacknowledged.__doc__ = list_unacknowledged.__doc__

    mcp.tool(
        name="list_unacknowledged",
        description=list_unacknowledged.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("list_unacknowledged")(_mcp_list_unacknowledged))

    async def _mcp_acknowledge_event(
        event_id: str,
        message: str = "",
        close: bool = False,
    ) -> dict:
        return await acknowledge_event(
            event_id=event_id,
            message=message,
            close=close,
            zabbix=get_zabbix(),
        )

    _mcp_acknowledge_event.__doc__ = acknowledge_event.__doc__

    mcp.tool(
        name="acknowledge_event",
        description=acknowledge_event.__doc__,
        annotations=ToolAnnotations(destructiveHint=True),
    )(_wrap("acknowledge_event")(_mcp_acknowledge_event))

    async def _mcp_batch_acknowledge(
        event_ids: list[str],
        message: str = "",
        close: bool = False,
    ) -> dict:
        return await batch_acknowledge(
            event_ids=event_ids,
            message=message,
            close=close,
            zabbix=get_zabbix(),
        )

    _mcp_batch_acknowledge.__doc__ = batch_acknowledge.__doc__

    mcp.tool(
        name="batch_acknowledge",
        description=batch_acknowledge.__doc__,
        annotations=ToolAnnotations(destructiveHint=True),
    )(_wrap("batch_acknowledge")(_mcp_batch_acknowledge))
