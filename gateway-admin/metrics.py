"""Prometheus HTTP API client.

Queries gateway-proxy's /metrics (port 9464) via the Prometheus query
API to build dashboard aggregations. Falls back to 0 when the proxy is
unreachable or has no data yet.
"""
import os
import httpx
import structlog

logger = structlog.get_logger()

PROMETHEUS_URL = os.environ.get(
    "GATEWAY_PROXY_METRICS_URL", "http://localhost:9464/metrics"
)
# The query API lives at /api/v1/query on the same host:port.
_QUERY_API = PROMETHEUS_URL.rsplit("/metrics", 1)[0] + "/api/v1/query"


async def query_prometheus(query: str) -> float:
    """Run an instant PromQL query, return the first result as float (0 if none)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(_QUERY_API, params={"query": query})
            data = resp.json()
            result = data.get("data", {}).get("result", [])
            if not result:
                return 0.0
            # instant query: result[0]["value"] = [timestamp, "string"]
            return float(result[0]["value"][1])
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        logger.warning("prometheus_query_failed", query=query, error=str(e), service="gateway-admin")
        return 0.0


async def query_prometheus_range(query: str, start: float, end: float, step: str) -> list[float]:
    """Run a range PromQL query, return a list of values."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _QUERY_API.replace("/query", "/query_range"),
                params={"query": query, "start": start, "end": end, "step": step},
            )
            data = resp.json()
            result = data.get("data", {}).get("result", [])
            if not result:
                return []
            return [float(v[1]) for v in result[0].get("values", [])]
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        logger.warning("prometheus_range_failed", query=query, error=str(e), service="gateway-admin")
        return []
