"""Failure audit logging to a Redis Stream.

The proxy writes one entry per failed request, including the full request
journey (which stage failed + per-stage timing). The admin service reads
this stream to populate the dashboard failure feed + trace view.
"""
import json
import structlog
from redis_client import get_redis
from db import get_pool

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
    meta: {trace_id, server, tool, op, message, latency_ms, token_name, time}
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
                "token_name": meta.get("token_name", ""),
                "time": meta["time"],
                "journey": json.dumps(journey),
            },
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as e:
        # NOTE: audit must never break the request path; log and continue.
        logger.error("audit_write_failed", error=str(e), service="gateway-proxy")


async def record_call(meta: dict, status: str, error_type: str | None = None) -> None:
    """INSERT 调用记录到 MySQL calls 表。旁路：失败仅记日志不阻断。"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO calls (time, server, tool, op, token_name, "
                    "latency_ms, status, error_type, trace) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (meta["time"], meta["server"], meta["tool"], meta["op"],
                     meta["token_name"], meta["latency_ms"], status,
                     error_type or "", meta["trace_id"]),
                )
    except Exception as e:
        logger.error("audit_call_write_failed", error=str(e), service="gateway-proxy")
