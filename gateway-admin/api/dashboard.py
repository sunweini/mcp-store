"""Dashboard API: metrics from MySQL + failures from Redis Stream.

Metrics aggregate from MySQL calls table (survives restart). Failures come from
the audit:failures Redis Stream written by gateway-proxy's audit module.
"""
import json
from fastapi import APIRouter, Depends, Query

from auth import require_admin
from db import get_pool
from redis_client import get_redis

router = APIRouter(prefix="/api", tags=["dashboard"])

# 24h 窗口常量，内联到 SQL 的 WHERE 子句中
WINDOW_24H = "DATE_SUB(NOW(), INTERVAL 24 HOUR)"


@router.get("/metrics/summary")
async def metrics_summary(server: str | None = None, _: str = Depends(require_admin)):
    """Aggregated request/error/latency stats from MySQL calls table.

    单条 SQL 聚合 COUNT/SUM/AVG/MAX + 二次查询排序取 P95 近似值。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            where = f"WHERE time > {WINDOW_24H}" + (" AND server=%s" if server else "")
            args = [server] if server else []
            await cur.execute(
                f"SELECT COUNT(*), SUM(status='fail'), AVG(latency_ms), MAX(latency_ms), "
                f"SUM(op='read'), SUM(op='write') FROM calls {where}", args)
            total, errors, avg_lat, max_lat, reads, writes = await cur.fetchone()
            # P95: ORDER BY latency_ms 取第 95 百分位（24h 窗口量级可接受）
            await cur.execute(
                f"SELECT latency_ms FROM calls {where} ORDER BY latency_ms", args)
            lats = [r[0] for r in await cur.fetchall()]
            p95 = lats[int(len(lats) * 0.95)] if lats else 0
    return {
        "requests": int(total or 0),
        "errors": int(errors or 0),
        "error_rate": round((errors or 0) / total * 100, 2) if total else 0.0,
        "p95_ms": int(p95),
        "avg_ms": int(avg_lat or 0),
        # 保持前端兼容：calls 表含 op 字段可聚合 read/write
        "read": int(reads or 0),
        "write": int(writes or 0),
        # calls 表不含 auth 阶段拒绝（未进入 tools/call），留 0 保持前端兼容
        "auth_failures": 0,
    }


@router.get("/metrics/by-server")
async def metrics_by_server(_: str = Depends(require_admin)):
    """Per-server stats table from MySQL calls table."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT server, COUNT(*), SUM(status='fail') FROM calls "
                f"WHERE time > {WINDOW_24H} GROUP BY server")
            rows = await cur.fetchall()
    return [{"server": r[0], "requests": int(r[1]), "errors": int(r[2] or 0),
             "error_rate": round((r[2] or 0) / r[1] * 100, 2) if r[1] else 0.0}
            for r in rows]


@router.get("/metrics/timeseries")
async def metrics_timeseries(server: str | None = None, window: str = "1h",
                             _: str = Depends(require_admin)):
    """Request-count time series, bucketed by minute."""
    interval = "1 HOUR" if window == "1h" else "24 HOUR"
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            where = f"WHERE time > DATE_SUB(NOW(), INTERVAL {interval})"
            if server:
                where += " AND server=%s"
            await cur.execute(
                f"SELECT FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(time)/60)*60) AS bucket, "
                f"COUNT(*), SUM(status='fail') FROM calls {where} "
                f"GROUP BY bucket ORDER BY bucket",
                [server] if server else [])
            rows = await cur.fetchall()
    return [{"time": str(r[0]), "requests": int(r[1]), "errors": int(r[2] or 0)}
            for r in rows]


@router.get("/failures")
async def list_failures(
    server: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: str = Depends(require_admin),
):
    """Read failed requests from the audit:failures Redis Stream (newest first)."""
    r = get_redis()
    # XREVRANGE returns newest first; +offset for pagination
    entries = await r.xrevrange("audit:failures", count=limit + offset)
    entries = entries[offset:]  # skip offset
    out = []
    for _id, fields in entries:
        rec = {
            "trace": fields.get("trace", ""),
            "server": fields.get("server", ""),
            "tool": fields.get("tool", ""),
            "op": fields.get("op", ""),
            "error_type": fields.get("error_type", ""),
            "message": fields.get("message", ""),
            "latency_ms": int(fields["latency_ms"]) if fields.get("latency_ms", "").isdigit() else 0,
            "time": fields.get("time", ""),
            "journey": json.loads(fields.get("journey", "[]")),
        }
        if server is None or rec["server"] == server:
            out.append(rec)
    return out
