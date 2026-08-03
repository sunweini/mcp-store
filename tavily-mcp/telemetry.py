"""OpenTelemetry TracerProvider + search-metrics setup.

镜像 zabbix-mcp/telemetry.py；指标按 spec 可观测性节：
- SEARCH_REQUESTS_TOTAL{provider, engine, status} — status 低基数: success/error
- SEARCH_QUOTA_REMAINING{provider} — 池内最低剩余
- SEARCH_QUOTA_RATIO{provider, level} — warning<10%/critical<5%/exhausted=0（按 provider 聚合，无 key label）
- SEARCH_KEY_POOL_SIZE{provider}, SEARCH_KEY_INVALID_TOTAL{provider}
- SEARCH_REQUEST_DURATION histogram — bucket 对齐 SLO: 0.1/0.5/1/3/5

OBS: key_id / 明文 key 不得进入 metric label（高基数+敏感，OBS-CORE-003）。
"""
import os

import structlog
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = structlog.get_logger()

PROMETHEUS_PORT = int(os.environ.get("PROMETHEUS_PORT", "9464"))

# Module-level instruments — guard with `if metric:` (may be None before init)
SEARCH_REQUESTS_TOTAL = None
SEARCH_QUOTA_REMAINING = None
SEARCH_QUOTA_RATIO = None
SEARCH_KEY_POOL_SIZE = None
SEARCH_KEY_INVALID_TOTAL = None
SEARCH_REQUEST_DURATION = None


def init_telemetry(service_name: str) -> None:
    """Initialize OTel + Prometheus metrics (same pattern as zabbix-mcp).

    Env vars:
    - OTEL_EXPORTER_OTLP_ENDPOINT: OTLP collector URL（未设则 console 输出 span）
    - OTEL_SERVICE_NAME: 覆盖默认服务名
    - PROMETHEUS_PORT: /metrics HTTP 端口（默认 9464）
    """
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})
    # Traces: OTLP exporter if configured, else ConsoleSpanExporter
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
    else:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        span_exporter = ConsoleSpanExporter()
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)

    # Metrics: Prometheus reader
    try:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader

        reader = PrometheusMetricReader()
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(meter_provider)
        meter = metrics.get_meter(service_name)

        # global 必须声明在赋值前，否则 Python 按局部变量解析报
        # UnboundLocalError（brief 代码的 global 位置有误，此处修正）
        global SEARCH_REQUESTS_TOTAL, SEARCH_QUOTA_REMAINING, SEARCH_QUOTA_RATIO
        global SEARCH_KEY_POOL_SIZE, SEARCH_KEY_INVALID_TOTAL, SEARCH_REQUEST_DURATION
        SEARCH_REQUESTS_TOTAL = meter.create_counter(
            "search_requests_total", unit="1", description="Search requests by provider/engine/status")
        SEARCH_QUOTA_REMAINING = meter.create_up_down_counter(
            "search_quota_remaining", unit="1", description="Lowest remaining quota in pool (provider)")
        SEARCH_QUOTA_RATIO = meter.create_up_down_counter(
            "search_quota_ratio", unit="1", description="Quota ratio bucket by provider (warning/critical/exhausted)")
        SEARCH_KEY_POOL_SIZE = meter.create_up_down_counter(
            "search_key_pool_size", unit="1", description="Active keys in pool (provider)")
        SEARCH_KEY_INVALID_TOTAL = meter.create_counter(
            "search_key_invalid_total", unit="1", description="Keys marked invalid (provider)")
        SEARCH_REQUEST_DURATION = meter.create_histogram(
            "search_request_duration_seconds", unit="s",
            description="Search request latency",
            # OTel SDK >=1.44: 参数名是 explicit_bucket_boundaries_advisory
            #（旧名 explicit_bucket_boundaries 已移除，传旧名报 TypeError）
            explicit_bucket_boundaries_advisory=[0.1, 0.5, 1.0, 3.0, 5.0])

        _start_prometheus_server(service_name)
    except Exception as e:
        logger.warning("telemetry_metrics_disabled", service=service_name, error=str(e))


def _start_prometheus_server(service_name: str) -> None:
    """Start HTTP server exposing /metrics for Prometheus scraping."""
    try:
        from prometheus_client import start_http_server
        start_http_server(PROMETHEUS_PORT)
        logger.info("otel_metrics_configured", service=service_name,
                    exporter="prometheus", port=PROMETHEUS_PORT)
    except Exception as e:
        # 端口占用等不致命——metrics 降级为 no-op，服务照常
        logger.warning("prometheus_server_start_failed",
                       service=service_name, error=str(e), port=PROMETHEUS_PORT)
