"""Prometheus text format parser + 审计消费者 metrics 初始化。

解析 gateway-proxy 的 /metrics endpoint（Prometheus text format）并取值，
无需独立 Prometheus server；同时承载消费者自身的三 instrument 初始化
（init_audit_metrics，按需启用）。
"""
import os
import re
import httpx
import structlog

logger = structlog.get_logger()

PROMETHEUS_URL = os.environ.get(
    "GATEWAY_PROXY_METRICS_URL", "http://localhost:9464/metrics"
)

# ── 审计消费者指标（spec 2026-08-07 concurrency-hardening 新增）─────────────
# 本文件原有职责是解析 proxy 的 Prometheus 文本，没有自己的 meter。消费者
# 侧指标按 gateway-proxy/observability.py 同款模式：模块级 instrument 占位
# None，init_audit_metrics() 一次性初始化；使用方运行时属性取值（不能
# from-import），instrument 未初始化时静默跳过——metrics 缺失绝不拖垮消费者。
# 三个 instrument 全部低基数（无 label），记录值本身是 batch 大小/耗时/深度。
AUDIT_BATCH_SIZE = None
AUDIT_BATCH_LATENCY = None
AUDIT_QUEUE_DEPTH = None


def init_audit_metrics() -> None:
    """初始化消费者三 instrument。幂等；OTel SDK 不可用时保持 None。"""
    global AUDIT_BATCH_SIZE, AUDIT_BATCH_LATENCY, AUDIT_QUEUE_DEPTH
    if AUDIT_BATCH_SIZE is not None:
        return
    try:
        from opentelemetry import metrics as _otel_metrics
        meter = _otel_metrics.get_meter("mcp-gateway-admin")
        AUDIT_BATCH_SIZE = meter.create_histogram(
            "audit_batch_size", description="Audit consumer batch size")
        AUDIT_BATCH_LATENCY = meter.create_histogram(
            "audit_batch_latency_seconds", description="Audit consumer insert latency")
        AUDIT_QUEUE_DEPTH = meter.create_histogram(
            "audit_queue_depth", description="Audit stream pending depth at consume")
    except Exception as e:
        # admin 无 OTel 依赖时保持 None，消费者照常跑（审计可丢 D4，指标更可丢）
        logger.warning("audit_metrics_init_failed", error=str(e), service="gateway-admin")


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
