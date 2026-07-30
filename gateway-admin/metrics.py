"""Prometheus text format parser.

Fetches gateway-proxy's /metrics endpoint (Prometheus text format) and
parses metric values. No separate Prometheus server needed.
"""
import os
import re
import httpx
import structlog

logger = structlog.get_logger()

PROMETHEUS_URL = os.environ.get(
    "GATEWAY_PROXY_METRICS_URL", "http://localhost:9464/metrics"
)


def _parse_prometheus_text(text: str) -> dict[str, list[tuple[dict, float]]]:
    """Parse Prometheus text format into {metric_name: [(labels, value), ...]}.

    Format:
      # HELP metric_name description
      # TYPE metric_name counter
      metric_name{label1="v1",label2="v2"} 123.0
    """
    metrics: dict[str, list[tuple[dict, float]]] = {}
    for line in text.strip().split("\n"):
        if not line or line.startswith("#"):
            continue
        # Parse: metric_name{labels} value
        m = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([0-9.eE+-]+)$', line)
        if not m:
            continue
        name, labels_str, value_str = m.groups()
        labels = {}
        if labels_str:
            # Parse {label1="v1",label2="v2"}
            for lm in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"', labels_str):
                labels[lm.group(1)] = lm.group(2)
        try:
            value = float(value_str)
        except ValueError:
            continue
        metrics.setdefault(name, []).append((labels, value))
    return metrics


async def fetch_metrics() -> dict[str, list[tuple[dict, float]]]:
    """Fetch and parse /metrics endpoint."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(PROMETHEUS_URL)
            return _parse_prometheus_text(resp.text)
    except httpx.HTTPError as e:
        logger.warning("prometheus_fetch_failed", error=str(e), service="gateway-admin")
        return {}


def sum_metric(metrics: dict, name: str, label_filters: dict | None = None) -> float:
    """Sum a metric, optionally filtering by labels."""
    total = 0.0
    for labels, value in metrics.get(name, []):
        if label_filters:
            if all(labels.get(k) == v for k, v in label_filters.items()):
                total += value
        else:
            total += value
    return total


async def query_prometheus(query: str) -> float:
    """Compatibility wrapper: parse simple sum queries from text format.

    Supports:
      - sum(metric_name)
      - sum(metric_name{label="value"})
      - metric_name (bare)
    """
    metrics = await fetch_metrics()
    if not metrics:
        return 0.0

    # Parse simple queries
    # sum(metric_name{label="value"}) or sum(metric_name)
    m = re.match(r'^sum\(([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\)$', query)
    if m:
        name, labels_str = m.groups()
        labels = {}
        if labels_str:
            for lm in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"', labels_str):
                labels[lm.group(1)] = lm.group(2)
            # Handle != operator
            for lm in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)!="([^"]*)"', labels_str):
                # For !=, we need to sum all EXCEPT this value
                # This is complex; for now just handle = operator
                pass
        return sum_metric(metrics, name, labels if labels else None)

    # Bare metric name
    m = re.match(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)$', query)
    if m:
        return sum_metric(metrics, m.group(1))

    # histogram_quantile and other complex queries - return 0 for now
    logger.debug("unsupported_query", query=query, service="gateway-admin")
    return 0.0


async def query_prometheus_range(query: str, start: float, end: float, step: str) -> list[float]:
    """Range queries not supported with text format. Returns empty list."""
    # Text format only gives current values, not time series
    # For time series, need a real Prometheus server
    return []
