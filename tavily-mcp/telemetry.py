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

# 配额告警档位（与 key_pool 低配额阈值对齐）：
# - warning: remaining/quota < 10%（low_quota_warning，仍正常参与轮询）
# - critical: remaining/quota < 5%（low_quota，跳过仅兜底）
# - exhausted: 无可用 remaining（0）——最低档，告警最重
# SEARCH_QUOTA_RATIO 的 level label 必须是这三个取值之一，取值规则
# 见 record_quota_metrics 的注释（为什么按该源最低档位聚合）。
QUOTA_LEVEL_WARNING = "warning"
QUOTA_LEVEL_CRITICAL = "critical"
QUOTA_LEVEL_EXHAUSTED = "exhausted"

# Module-level instruments — guard with `if metric:` (may be None before init)
SEARCH_REQUESTS_TOTAL = None
SEARCH_QUOTA_REMAINING = None
SEARCH_QUOTA_RATIO = None
SEARCH_KEY_POOL_SIZE = None
SEARCH_KEY_INVALID_TOTAL = None
SEARCH_REQUEST_DURATION = None

_initialized = False


def record_quota_metrics(provider: str, snapshot: dict) -> None:
    """写池健康摘要到配额指标（I-1 接线入口）。

    snapshot 由 KeyPool.health_snapshot() 产出：{'lowest_ratio', 'lowest_remaining',
    'pool_size', 'invalid_count'}。调用时机（KeyPool reload/on_error(INVALID) 后）
    保证告警数据随池状态变化刷新，而非只在进程启动时发一次。

    SEARCH_QUOTA_RATIO 的 level 取该源最低 remaining 的档位（spec 可观测性
    节：按 provider 聚合，避免 key 级高基数 label）：
    - lowest_ratio <= 0（或 None 但 remaining 为 0）→ exhausted
    - lowest_ratio < 5% → critical
    - lowest_ratio < 10% → warning
    无任何可计算 ratio 的 key 时只发 pool_size/invalid（不编造 level）。

    用 up_down_counter 的理由：ratio 档位会在 warning → critical → 正常 之间
    反复横跳（配额变化/补 key），counter 绝对值无意义，需要的是「当前档位」
    这种可升降的状态——up_down_counter 语义正好（set 由 add 差量模拟，
    详见下方注释）。
    """
    metrics_set = {
        SEARCH_QUOTA_REMAINING, SEARCH_QUOTA_RATIO,
        SEARCH_KEY_POOL_SIZE, SEARCH_KEY_INVALID_TOTAL,
    }
    if not any(metrics_set):
        # telemetry 未初始化（metrics 降级）时全为 None，直接返回——
        # 避免每 key 事件白走一轮空转
        return

    if SEARCH_KEY_POOL_SIZE:
        SEARCH_KEY_POOL_SIZE.add(snapshot["pool_size"], attributes={"provider": provider})
    if SEARCH_KEY_INVALID_TOTAL:
        SEARCH_KEY_INVALID_TOTAL.add(snapshot["invalid_count"], attributes={"provider": provider})
    if SEARCH_QUOTA_REMAINING:
        lowest = snapshot["lowest_remaining"]
        if lowest is not None:
            SEARCH_QUOTA_REMAINING.add(lowest, attributes={"provider": provider})

    # up_down_counter 的 add(absolute) 语义：prometheus_exporter 对
    # up_down_counter 的 add(v) 直接 set 为 v（counter 才做累加）——
    # 这里传入的是绝对档位值而非增量，见下注释。
    level = None
    if snapshot.get("lowest_ratio") is not None:
        if snapshot["lowest_ratio"] <= 0:
            level = QUOTA_LEVEL_EXHAUSTED
        elif snapshot["lowest_ratio"] < 0.05:
            level = QUOTA_LEVEL_CRITICAL
        elif snapshot["lowest_ratio"] < 0.10:
            level = QUOTA_LEVEL_WARNING
    # lowest_ratio None 但 remaining==0：低配 key 无 quota 数据（remaining
    # 未知 key 不触发阈值），但剩余为 0 应视为耗尽——两处数据同源冗余兜底
    if level is None and snapshot.get("lowest_remaining") == 0:
        level = QUOTA_LEVEL_EXHAUSTED
    if level is not None and SEARCH_QUOTA_RATIO:
        SEARCH_QUOTA_RATIO.add(1, attributes={"provider": provider, "level": level})


def init_telemetry(service_name: str) -> None:
    """Initialize OTel + Prometheus metrics (same pattern as zabbix-mcp).

    Env vars:
    - OTEL_EXPORTER_OTLP_ENDPOINT: OTLP collector URL（未设则 console 输出 span）
    - OTEL_SERVICE_NAME: 覆盖默认服务名
    - PROMETHEUS_PORT: /metrics HTTP 端口（默认 9464）
    """
    # 幂等 guard（I4）：重复调用会重复注册 meter 与 start_http_server
    # （第二次端口占用被吞但造成双注册）。测试多次 import / 未来
    # gateway 组合加载时都只会初始化一次。
    # global 必须声明在赋值前（Python 语法规则），统一放函数顶部。
    global SEARCH_REQUESTS_TOTAL, SEARCH_QUOTA_REMAINING, SEARCH_QUOTA_RATIO
    global SEARCH_KEY_POOL_SIZE, SEARCH_KEY_INVALID_TOTAL, SEARCH_REQUEST_DURATION
    global _initialized
    if _initialized:
        return

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
        _initialized = True
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
