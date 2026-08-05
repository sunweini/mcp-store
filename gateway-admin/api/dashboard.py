"""Dashboard API: metrics + failures 均读 MySQL calls 表。

Metrics aggregate from MySQL calls table (survives restart). Failures 面板
也改读 calls 表（status='fail'），与总请求数同源一致；Redis audit:failures
流仍由 proxy 双写，仅作回滚兜底，admin 不再读。
"""
import json
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query

from auth import require_admin
from db import get_pool

router = APIRouter(prefix="/api", tags=["dashboard"])

# 24h 窗口常量，内联到 SQL 的 WHERE 子句中（白名单常量，无注入风险）
WINDOW_24H = "DATE_SUB(NOW(), INTERVAL 24 HOUR)"


def _p95(lats: list) -> int:
    """从已排序的 latency 列表取 P95 近似值（24h 窗口量级可接受）。"""
    if not lats:
        return 0
    return int(lats[int(len(lats) * 0.95)])


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
                f"SUM(op='read'), SUM(op='write'), "
                f"SUM(error_type IN ('invalid_token', 'permission_denied')) "
                f"FROM calls {where}", args)
            total, errors, avg_lat, max_lat, reads, writes, auth_failures = await cur.fetchone()
            # P95: ORDER BY latency_ms 取第 95 百分位（24h 窗口量级可接受）
            await cur.execute(
                f"SELECT latency_ms FROM calls {where} ORDER BY latency_ms", args)
            lats = [r[0] for r in await cur.fetchall()]
            p95 = _p95(lats)
    return {
        "requests": int(total or 0),
        "errors": int(errors or 0),
        "error_rate": round((errors or 0) / total * 100, 2) if total else 0.0,
        "p95_ms": p95,
        "avg_ms": int(avg_lat or 0),
        # 保持前端兼容：calls 表含 op 字段可聚合 read/write
        "read": int(reads or 0),
        "write": int(writes or 0),
        # calls 表含 invalid_token/permission_denied 拒绝记录（on_call_tool 拒绝路径写入）
        "auth_failures": int(auth_failures or 0),
    }


@router.get("/metrics/by-server")
async def metrics_by_server(_: str = Depends(require_admin)):
    """Per-server stats table from MySQL calls table.

    2 查询替代 N+1：先 GROUP BY server 取聚合，再取全部 latency 按 server 分组算 P95。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT server, COUNT(*), SUM(status='fail') FROM calls "
                f"WHERE time > {WINDOW_24H} GROUP BY server")
            rows = await cur.fetchall()
            # P95：取所有 server 的 latency（ORDER BY server, latency_ms 已排序），Python 分组算
            await cur.execute(
                f"SELECT server, latency_ms FROM calls "
                f"WHERE time > {WINDOW_24H} ORDER BY server, latency_ms")
            lat_by_server: dict[str, list[int]] = {}
            for srv, lat in await cur.fetchall():
                lat_by_server.setdefault(srv, []).append(lat)
    return [{"server": r[0], "requests": int(r[1]), "errors": int(r[2] or 0),
             "error_rate": round((r[2] or 0) / r[1] * 100, 2) if r[1] else 0.0,
             "p95_ms": _p95(lat_by_server.get(r[0], []))} for r in rows]


@router.get("/metrics/timeseries")
async def metrics_timeseries(server: str | None = None, window: str = "1h",
                             _: str = Depends(require_admin)):
    """Request-count time series, bucketed by minute(1h) or hour(24h).

    返回 {window, points: [float]} 保持前端兼容。points 是每桶请求数，
    Python 生成连续桶填 0 避免 sparkline 断点。
    """
    # 1h 窗口按分钟桶（60 点），24h 窗口按小时桶（24 点）
    if window == "1h":
        bucket_sec = 60
        num_buckets = 60
        interval = "1 HOUR"
    else:
        bucket_sec = 3600
        num_buckets = 24
        interval = "24 HOUR"

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            where = f"WHERE time > DATE_SUB(NOW(), INTERVAL {interval})"
            if server:
                where += " AND server=%s"
            await cur.execute(
                f"SELECT FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(time)/{bucket_sec})*{bucket_sec}) AS bucket, "
                f"COUNT(*) FROM calls {where} GROUP BY bucket ORDER BY bucket",
                [server] if server else [])
            rows = await cur.fetchall()

    # 连续桶填 0：datetime.now() 与 MySQL NOW() 同时区，桶字符串可匹配
    counts = {str(r[0]): float(r[1]) for r in rows}
    now = datetime.now()
    start = now - timedelta(seconds=num_buckets * bucket_sec)
    # 对齐到桶边界（分钟桶截到整分，小时桶截到整点）
    if bucket_sec == 60:
        start = start.replace(second=0, microsecond=0)
    else:
        start = start.replace(minute=0, second=0, microsecond=0)

    points = []
    for i in range(num_buckets):
        bucket_time = start + timedelta(seconds=i * bucket_sec)
        bucket_str = bucket_time.strftime("%Y-%m-%d %H:%M:%S")
        points.append(counts.get(bucket_str, 0.0))

    return {"window": window, "points": points}


@router.get("/failures")
async def list_failures(
    server: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: str = Depends(require_admin),
):
    """Failed requests from MySQL calls table (status='fail'), newest first.

    为什么改读 MySQL：失败面板与总请求/错误数统计（同表聚合）同源，
    口径必然一致；Redis 流有 MAXLEN 截断且重启丢数据，仅作回滚兜底。
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            where = "WHERE status = %s"
            args: list = ["fail"]
            if server:
                where += " AND server = %s"
                args.append(server)
            # ORDER BY id DESC：自增 id 与写入顺序一致，比 time 排序更便宜
            # （time 无索引覆盖排序，id 是主键倒序扫描）
            await cur.execute(
                f"SELECT trace, server, tool, op, error_type, message, "
                f"latency_ms, time, journey FROM calls {where} "
                f"ORDER BY id DESC LIMIT %s OFFSET %s",
                args + [limit, offset],
            )
            rows = await cur.fetchall()

    out = []
    for trace, srv, tool, op, error_type, message, latency_ms, t, journey in rows:
        try:
            parsed_journey = json.loads(journey) if journey else []
        except (TypeError, ValueError):
            # 脏数据防御：历史行/手工改坏的 journey 不能让面板 500
            parsed_journey = []
        out.append({
            "trace": trace or "",
            "server": srv or "",
            "tool": tool or "",
            "op": op or "",
            "error_type": error_type or "",
            "message": message or "",
            "latency_ms": int(latency_ms or 0),
            # calls 表 time 是 UTC 墙钟（proxy 用 gmtime 写入）；格式化成
            # Redis 时代的 ISO+Z 字符串，前端 ago() 的 new Date() 按 UTC 解析
            # 才不失真（缺 Z 会按浏览器本地时区偏移）
            "time": t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else "",
            "journey": parsed_journey,
        })
    return out
