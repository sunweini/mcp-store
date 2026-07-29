"""Alert patrol tools — query active problems and generate summaries.

Read-only operations (readOnlyHint=True): safe for AI to auto-execute.
Results sorted by time descending (newest first) per spec requirement.

Design note: functions are defined at module level (not as closures inside
register()) so tests can import and call them directly with a mock zabbix
client. register() creates thin MCP wrappers that inject the real client.
"""
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
import structlog

from zabbix_client import ZabbixClient, ZabbixAPIError, ZabbixConnectionError, SEVERITY_MAP

logger = structlog.get_logger()

# Reverse map: severity name → Zabbix integer (e.g. "high" → 4)
# Built once at import time from the canonical SEVERITY_MAP
_SEVERITY_NAME_TO_INT = {v: k for k, v in SEVERITY_MAP.items()}


def _resolve_severity(name: str | None) -> int | None:
    """Convert severity name (e.g. 'high') to Zabbix integer (e.g. 4).

    Returns None when name is None or not a valid Zabbix severity —
    caller decides whether to treat invalid input as error or ignore.
    """
    if name is None:
        return None
    return _SEVERITY_NAME_TO_INT.get(name.lower())


async def _resolve_trigger_hosts(
    problems: list[dict], zabbix: ZabbixClient
) -> dict[str, list[str]]:
    """Batch-resolve host names for trigger-based problems via trigger.get.

    Zabbix 6.4 problem.get returns hosts=None for trigger problems.
    We resolve via trigger.get using the problem's objectid (= triggerid).

    Returns: {triggerid: ["host1", "host2", ...], ...}
    """
    # Collect unique trigger IDs (objectid for source=0 problems)
    trigger_ids = list({
        p["objectid"] for p in problems
        if p.get("source") == "0" and p.get("objectid")
    })
    if not trigger_ids:
        return {}

    try:
        triggers = await zabbix.call(
            "trigger.get",
            {"triggerids": trigger_ids, "output": ["triggerid"], "selectHosts": ["name"]},
        )
    except (ZabbixAPIError, ZabbixConnectionError):
        # Fallback: return empty map, host will show as "unknown"
        return {}

    return {
        t["triggerid"]: [h["name"] for h in t.get("hosts", [])]
        for t in triggers
    }


def _format_problem(p: dict, host_map: dict[str, list[str]] | None = None) -> dict:
    """Format a raw Zabbix problem object into the tool response schema.

    host_map: optional {triggerid: [host_names]} from _resolve_trigger_hosts.
    Falls back to problem.get's hosts field, then "unknown".
    """
    sev_int = int(p.get("severity", 0))
    objectid = p.get("objectid", "")

    # Resolve host name: host_map > problem hosts > "unknown"
    host_name = "unknown"
    if host_map and objectid in host_map and host_map[objectid]:
        host_name = host_map[objectid][0]
    elif p.get("hosts"):
        host_name = p["hosts"][0]["name"]

    groups = p.get("groups", [])
    return {
        "event_id": p.get("eventid"),
        "host": host_name,
        "description": p.get("name", ""),
        "severity": sev_int,
        "severity_name": SEVERITY_MAP.get(sev_int, "unknown"),
        "clock": p.get("clock"),
        "acknowledged": p.get("acknowledged") == "1",
        "groups": [g["name"] for g in groups],
    }


# ── Module-level tool implementations ──────────────────────────────────────────
# These take zabbix as an explicit keyword-only parameter so tests can inject
# a mock client. register() wraps them to inject the real client from app state.


async def list_active_problems(
    severity: str | None = None,
    host_group: str | None = None,
    host: str | None = None,
    limit: int = 50,
    *,
    zabbix: ZabbixClient | None = None,
) -> dict:
    """查询当前未恢复的活跃告警，按时间降序（最新在前）。

    返回每条告警的：主机名、触发器描述、严重级别（数字+名称）、发生时间、是否已确认。
    severity 可选值: not_classified, information, warning, average, high, disaster
    """
    # Validate severity before touching the network — fail fast on bad input
    sev_int = _resolve_severity(severity)
    if severity is not None and sev_int is None:
        valid = ", ".join(sorted(_SEVERITY_NAME_TO_INT.keys()))
        return {
            "status": "error",
            "message": f"无效严重级别: '{severity}'。可选值: {valid}",
        }

    if zabbix is None:
        return {"status": "error", "message": "zabbix client not initialized"}

    params = {
        "output": "extend",
        "selectHosts": ["name"],
        "selectGroups": ["name"],
        "sortfield": "eventid",
        "sortorder": "DESC",
        "recent": True,
        "limit": limit,
    }
    if sev_int is not None:
        # Zabbix expects a list of severity integers for the severities filter
        params["severities"] = [sev_int]

    try:
        problems = await zabbix.call("problem.get", params)
    except (ZabbixAPIError, ZabbixConnectionError) as e:
        logger.error(
            "list_active_problems_zabbix_error",
            service="zabbix-mcp",
            error=str(e),
            severity=severity,
            host=host,
        )
        return {"status": "error", "message": str(e)}

    # Resolve host names via trigger.get (problem.get doesn't return hosts for triggers)
    host_map = await _resolve_trigger_hosts(problems, zabbix)
    data = [_format_problem(p, host_map) for p in problems]

    # Client-side host name filter — Zabbix problem.get only accepts hostids,
    # not host names. Resolving name→id requires an extra host.get round-trip,
    # so we filter in-process for the typical result sizes (<1000 problems).
    if host:
        data = [d for d in data if d["host"] == host]

    if host_group:
        data = [d for d in data if host_group in d["groups"]]

    return {"status": "ok", "data": data, "count": len(data)}


async def problem_summary(
    *,
    zabbix: ZabbixClient | None = None,
) -> dict:
    """生成告警摘要报告。

    返回：total（总数）、by_severity（按级别分布）、by_host_group（按主机组）、
    top_hosts（TOP 10 主机）、unacknowledged（未确认数）。
    """
    if zabbix is None:
        return {"status": "error", "message": "zabbix client not initialized"}

    params = {
        "output": "extend",
        "selectHosts": ["name"],
        "selectGroups": ["name"],
        "recent": True,
    }

    try:
        problems = await zabbix.call("problem.get", params)
    except (ZabbixAPIError, ZabbixConnectionError) as e:
        logger.error(
            "problem_summary_zabbix_error",
            service="zabbix-mcp",
            error=str(e),
        )
        return {"status": "error", "message": str(e)}

    # Resolve host names via trigger.get for accurate top_hosts
    host_map = await _resolve_trigger_hosts(problems, zabbix)

    # Single-pass aggregation — avoid iterating problems multiple times
    by_severity: dict[str, int] = {}
    by_host_group: dict[str, int] = {}
    host_counts: dict[str, int] = {}
    unacknowledged = 0

    for p in problems:
        sev_name = SEVERITY_MAP.get(int(p.get("severity", 0)), "unknown")
        by_severity[sev_name] = by_severity.get(sev_name, 0) + 1

        if p.get("acknowledged") == "0":
            unacknowledged += 1

        for g in p.get("groups", []):
            gname = g.get("name", "unknown")
            by_host_group[gname] = by_host_group.get(gname, 0) + 1

        # Use host_map for trigger-based problems
        objectid = p.get("objectid", "")
        if host_map and objectid in host_map and host_map[objectid]:
            for hname in host_map[objectid]:
                host_counts[hname] = host_counts.get(hname, 0) + 1
        else:
            for h in p.get("hosts", []) or []:
                hname = h.get("name", "unknown")
                host_counts[hname] = host_counts.get(hname, 0) + 1

    # Top 10 hosts by problem count — sorted descending
    top_hosts = sorted(host_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "status": "ok",
        "data": {
            "total": len(problems),
            "by_severity": by_severity,
            "by_host_group": by_host_group,
            "top_hosts": [{"host": h, "count": c} for h, c in top_hosts],
            "unacknowledged": unacknowledged,
        },
    }


# ── MCP registration ───────────────────────────────────────────────────────────


def register(mcp: FastMCP, get_zabbix, metrics=None) -> None:
    """Register problem tools on the FastMCP server.

    get_zabbix: callable returning the ZabbixClient from app state.
    Uses a closure to defer client lookup until tool invocation time —
    the client isn't available at import/registration time in stateless mode.

    metrics: optional _metrics_wrapper factory from tools/__init__.py.
    """
    _wrap = metrics or (lambda name: lambda f: f)

    # Wrapper functions have a different Python name than the MCP tool name.
    # mcp.tool(name=...) sets the visible tool name for LLM clients.
    # Docstrings are copied so FastMCP generates the correct description.

    async def _mcp_list_active_problems(
        severity: str | None = None,
        host_group: str | None = None,
        host: str | None = None,
        limit: int = 50,
    ) -> dict:
        return await list_active_problems(
            severity=severity,
            host_group=host_group,
            host=host,
            limit=limit,
            zabbix=get_zabbix(),
        )

    _mcp_list_active_problems.__doc__ = list_active_problems.__doc__

    mcp.tool(
        name="list_active_problems",
        description=list_active_problems.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("list_active_problems")(_mcp_list_active_problems))

    async def _mcp_problem_summary() -> dict:
        return await problem_summary(zabbix=get_zabbix())

    _mcp_problem_summary.__doc__ = problem_summary.__doc__

    mcp.tool(
        name="problem_summary",
        description=problem_summary.__doc__,
        annotations=ToolAnnotations(readOnlyHint=True),
    )(_wrap("problem_summary")(_mcp_problem_summary))
