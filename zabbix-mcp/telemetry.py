"""OpenTelemetry TracerProvider + Metrics setup.

Configures OTel SDK with OTLP exporter for traces and Prometheus for metrics.
Controlled via environment variables — no code changes needed to switch backends.

OBS-CORE-004: Logs/Traces/Metrics 职责分离。
OBS-MET-001: 7 个核心 Metrics。
OBS-MET-003: Histogram bucket 对齐 SLO。
"""
import os

import structlog
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = structlog.get_logger()

# NOTE: Prometheus HTTP endpoint for /metrics scraping.
# Prometheus pulls this endpoint every scrape_interval (default 15s).
PROMETHEUS_PORT = int(os.environ.get("PROMETHEUS_PORT", "9464"))


def init_telemetry(service_name: str = "zabbix-mcp") -> None:
    """Initialize OTel TracerProvider + Metrics.

    Env vars:
    - OTEL_EXPORTER_OTLP_ENDPOINT: OTLP collector URL (e.g. http://localhost:4317)
    - OTEL_SERVICE_NAME: service name (default: zabbix-mcp)
    - FASTMCP_TELEMETRY_MODE: native | propagation_only | off (default: native)
    - PROMETHEUS_PORT: metrics HTTP port (default: 9464)

    If OTEL_EXPORTER_OTLP_ENDPOINT is not set, traces use ConsoleSpanExporter
    (visible in stdout) — useful for local development without a collector.
    """
    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
    })

    # ── Traces ─────────────────────────────────────────────────────
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        logger.info(
            "otel_traces_configured",
            service=service_name,
            exporter="otlp",
            endpoint=otlp_endpoint,
        )
    else:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        span_exporter = ConsoleSpanExporter()
        logger.info(
            "otel_traces_configured",
            service=service_name,
            exporter="console",
            note="Set OTEL_EXPORTER_OTLP_ENDPOINT to export to a collector",
        )

    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)

    # ── Metrics ────────────────────────────────────────────────────
    _setup_metrics(resource, service_name)


def _setup_metrics(resource: Resource, service_name: str) -> None:
    """Configure Prometheus metrics exporter.

    OBS-MET-001: 7 core metrics only.
    OBS-MET-003: Histogram buckets aligned to SLO.
    OBS-MET-002: Low-cardinality labels only.
    """
    try:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from opentelemetry.sdk.metrics import Histogram, Counter, UpDownCounter

        reader = PrometheusMetricReader()
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[reader],
        )
        metrics.set_meter_provider(meter_provider)

        meter = metrics.get_meter("zabbix_mcp")

        # OBS-MET-001: 7 core metrics (service-level only)
        # NOTE: These are module-level globals so tool code can record to them.
        # Labels are low-cardinality (route, method, status_code) per OBS-MET-002.
        global REQUESTS_TOTAL, REQUEST_DURATION, ERRORS_TOTAL
        global DEPENDENCY_DURATION, DEPENDENCY_ERRORS_TOTAL
        global IN_FLIGHT_REQUESTS

        REQUESTS_TOTAL = meter.create_counter(
            name="zabbix_mcp_requests_total",
            description="Total MCP tool calls",
            unit="1",
        )
        REQUEST_DURATION = meter.create_histogram(
            name="zabbix_mcp_request_duration_seconds",
            description="MCP tool call latency",
            unit="s",
            # OBS-MET-003: custom buckets via OTel View (not inline)
        )
        ERRORS_TOTAL = meter.create_counter(
            name="zabbix_mcp_errors_total",
            description="Total MCP tool errors",
            unit="1",
        )
        DEPENDENCY_DURATION = meter.create_histogram(
            name="zabbix_mcp_dependency_duration_seconds",
            description="Zabbix API call latency",
            unit="s",
        )
        DEPENDENCY_ERRORS_TOTAL = meter.create_counter(
            name="zabbix_mcp_dependency_errors_total",
            description="Total Zabbix API errors",
            unit="1",
        )
        IN_FLIGHT_REQUESTS = meter.create_up_down_counter(
            name="zabbix_mcp_in_flight_requests",
            description="Currently processing MCP requests",
            unit="1",
        )

        # Start Prometheus HTTP server for /metrics endpoint
        _start_prometheus_server()

        logger.info(
            "otel_metrics_configured",
            service=service_name,
            exporter="prometheus",
            port=PROMETHEUS_PORT,
        )

    except ImportError:
        logger.warning(
            "prometheus_exporter_not_available",
            service=service_name,
            note="Install opentelemetry-exporter-prometheus for metrics",
        )


def _start_prometheus_server() -> None:
    """Start HTTP server exposing /metrics for Prometheus scraping."""
    try:
        from prometheus_client import start_http_server
        start_http_server(PROMETHEUS_PORT)
    except Exception as e:
        logger.warning(
            "prometheus_server_start_failed",
            error=str(e),
            port=PROMETHEUS_PORT,
        )
