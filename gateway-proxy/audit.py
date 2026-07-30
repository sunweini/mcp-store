"""Failure audit logging to a Redis Stream.

The proxy writes one entry per failed request, including the full request
journey (which stage failed + per-stage timing). The admin service reads
this stream to populate the dashboard failure feed + trace view.
"""
import json
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

# MAXLEN trims the stream so it cannot grow unbounded.
_STREAM_MAXLEN = 10000


async def record_failure(
    journey: list[dict],
    error_type: str,
    meta: dict,
) -> None:
    """Append a failure record to the audit:failures Redis Stream.

    journey: [{stage, state, ms}, ...] - state is ok|fail|skip
    error_type: one of ERROR_TYPES
    meta: {trace_id, server, tool, op, message, latency_ms, time}
    """
    r = get_redis()
    try:
        await r.xadd(
            "audit:failures",
            {
                "trace": meta["trace_id"],
                "server": meta["server"],
                "tool": meta["tool"],
                "op": meta["op"],
                "error_type": error_type,
                "message": meta["message"],
                "latency_ms": meta["latency_ms"],
                "time": meta["time"],
                "journey": json.dumps(journey),
            },
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as e:
        # NOTE: audit must never break the request path; log and continue.
        logger.error("audit_write_failed", error=str(e), service="gateway-proxy")
