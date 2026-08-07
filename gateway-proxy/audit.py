"""Failure audit: proxy 只 XADD audit:calls stream，MySQL 落库在 admin 消费者。

改造前 proxy 同步写 MySQL calls 表 + Redis audit:failures 双写；现在 MySQL
完全移出请求路径（D1/D3）——单流 audit:calls 承载成功+失败全量，消费者
（gateway-admin）XREADGROUP 批量落库。XADD 失败仅日志+指标（D4 审计可丢）。
"""
import structlog
from redis_client import get_redis

logger = structlog.get_logger()

# NOTE: bounded enum consumed by the admin frontend's error-type chips.
ERROR_TYPES = frozenset({
    "upstream_timeout",
    "permission_denied",
    "invalid_token",
    "upstream_error",
    "connection_error",
})

_STREAM = "audit:calls"
# MAXLEN trims the stream so it cannot grow unbounded (R9: 50000 条 = 千级 QPS 下 50s 缓冲)
_STREAM_MAXLEN = 50000


async def record_call_stream(
    meta: dict,
    status: str,
    error_type: str | None = None,
    message: str | None = None,
    journey: list | None = None,
) -> None:
    """Append one audit record to audit:calls stream. Never raises (D4)."""
    r = get_redis()
    try:
        await r.xadd(
            _STREAM,
            {
                "time": meta["time"],
                "server": meta["server"],
                "tool": meta["tool"],
                "op": meta["op"],
                "token_name": meta["token_name"],
                "latency_ms": str(meta["latency_ms"]),
                "status": status,
                "error_type": error_type or "",
                "message": message or "",
                "journey": __import__("json").dumps(journey or []),
                "trace": meta["trace_id"],
            },
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as e:
        # 审计绝不断请求路径；失败计入 audit_dropped_total 指标（observability 模块运行时取值）
        import observability
        if observability.AUDIT_DROPPED_TOTAL:
            observability.AUDIT_DROPPED_TOTAL.add(1, {})
        logger.error("audit_xadd_failed", error=str(e), service="gateway-proxy")
