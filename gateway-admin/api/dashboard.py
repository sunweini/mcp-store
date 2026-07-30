"""Dashboard API: metrics summary + failures from Redis Stream.

Metrics come from Prometheus (gateway-proxy :9464). Failures come from
the audit:failures Redis Stream written by gateway-proxy's audit module.
"""
import json
import time
from fastapi import APIRouter, Depends, Query

from auth import require_admin
from redis_client import get_redis
import metrics

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/metrics/summary")
async def metrics_summary(server: str | None = None, _: str = Depends(require_admin)):
    """Aggregated request/error/latency stats. Optional server filter."""
    label = f'{{server="{server}"}}' if server else ""
    req_filter = f"gateway_requests_total{label}" if label else "gateway_requests_total"
    requests = await metrics.query_prometheus(f"sum({req_filter})")
    errors = await metrics.query_prometheus(f"sum(gateway_requests_total{{status!='ok'}})")
    auth_failures = await metrics.query_prometheus("sum(gateway_auth_failures_total)")
    p95 = await metrics.query_prometheus("histogram_quantile(0.95, sum by (le) (gateway_request_duration_seconds_bucket))")
    reads = await metrics.query_prometheus('sum(gateway_requests_total{operation="read"})')
    writes = await metrics.query_prometheus('sum(gateway_requests_total{operation="write"})')
    error_rate = round(errors / requests * 100, 2) if requests else 0.0
    return {
        "requests": int(requests),
        "errors": int(errors),
        "error_rate": error_rate,
        "p95_ms": round(p95 * 1000, 1) if p95 else 0,
        "read": int(reads),
        "write": int(writes),
        "auth_failures": int(auth_failures),
    }


@router.get("/metrics/by-server")
async def metrics_by_server(_: str = Depends(require_admin)):
    """Per-server stats table."""
    r = get_redis()
    names = await r.smembers("servers:active")
    out = []
    for name in names:
        reqs = await metrics.query_prometheus(f'sum(gateway_requests_total{{server="{name}"}})')
        errs = await metrics.query_prometheus(f'sum(gateway_requests_total{{server="{name}",status!="ok"}})')
        p95 = await metrics.query_prometheus(
            f'histogram_quantile(0.95, sum by (le) (gateway_request_duration_seconds_bucket{{server="{name}"}}))'
        )
        out.append({
            "server": name,
            "requests": int(reqs),
            "errors": int(errs),
            "error_rate": round(errs / reqs * 100, 2) if reqs else 0.0,
            "p95_ms": round(p95 * 1000, 1) if p95 else 0,
        })
    return out


@router.get("/metrics/timeseries")
async def metrics_timeseries(server: str | None = None, window: str = "1h", _: str = Depends(require_admin)):
    """Request-count time series for sparkline/timeline."""
    end = time.time()
    start = end - 3600  # 1h; expand if window == "24h"
    if window == "24h":
        start = end - 86400
    label = f'{{server="{server}"}}' if server else ""
    q = f"sum(rate(gateway_requests_total{label}[1m]))"
    points = await metrics.query_prometheus_range(q, start, end, "60")
    return {"window": window, "bucket": "1min", "points": points}


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
