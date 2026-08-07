"""OTel TracerProvider + Prometheus metrics.

Mirrors the zabbix-mcp telemetry setup so the two services share a backend.
Metrics use the OTel SDK with a Prometheus exporter (NOT prometheus_client
directly), per the observability coding standard.
"""
import os
import structlog
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

logger = structlog.get_logger()

PROMETHEUS_PORT = int(os.environ.get("PROMETHEUS_PORT", "9464"))

# Module-level instruments; None until init_telemetry() runs.
REQUESTS_TOTAL = None
REQUEST_LATENCY = None
AUTH_FAILURES = None
AUDIT_DROPPED_TOTAL = None


def init_telemetry(service_name: str = "mcp-gateway") -> None:
    """Configure OTel traces + Prometheus metrics. Safe to call once at startup.

    OTEL_SDK_DISABLED=true 时直接跳过（压测/冒烟场景禁用 console span 导出，
    避免每请求输出大 JSON 拖垮吞吐；instrument 保持 None，middleware 侧已
    None-guard）。
    ⚠️ 防未来踩坑：本守卫早退时 instruments（REQUESTS_TOTAL 等）保持 None。
    若未来某调用点忘记 None-guard，此处会造成静默 None 调用——新增 instrument
    使用处必须带 None 检查。
    """
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        logger.info("telemetry_disabled", reason="OTEL_SDK_DISABLED", service=service_name)
        return
    global REQUESTS_TOTAL, REQUEST_LATENCY, AUTH_FAILURES, AUDIT_DROPPED_TOTAL

    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
    })

    # ── Traces ───────────────────────────────────────────────────
    otlp = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if otlp:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint=otlp)
    else:
        exporter = ConsoleSpanExporter()
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # ── Metrics ──────────────────────────────────────────────────
    try:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from prometheus_client import start_http_server

        reader = PrometheusMetricReader()
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
        start_http_server(PROMETHEUS_PORT)

        meter = metrics.get_meter("mcp-gateway")
        # NOTE: labels are bounded-cardinality (server/tool/operation/status)
        REQUESTS_TOTAL = meter.create_counter("gateway_requests_total", description="Total MCP requests")
        REQUEST_LATENCY = meter.create_histogram("gateway_request_duration_seconds", description="Request latency")
        AUTH_FAILURES = meter.create_counter("gateway_auth_failures_total", description="Auth failures")
        AUDIT_DROPPED_TOTAL = meter.create_counter("audit_dropped_total", description="Audit stream XADD failures")
        logger.info("metrics_configured", service=service_name, port=PROMETHEUS_PORT)
    except Exception as exc:
        # Catch OSError (port conflict on Linux/Docker) AND ImportError (missing
        # optional deps). Mirrors zabbix-mcp/telemetry.py _start_prometheus_server:
        # log a warning, never crash init_telemetry() -> server stays up.
        logger.warning(
            "metrics_init_failed",
            service=service_name,
            error=str(exc),
            error_type=type(exc).__name__,
        )
