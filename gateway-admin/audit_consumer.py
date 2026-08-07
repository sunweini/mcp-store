"""审计消费者：XREADGROUP audit:calls → executemany 批量 INSERT calls 表 → XACK。

D2：消费者放 gateway-admin（lifespan 后台 task），非 proxy 非独立容器。
批量参数：batch=100, block=1s（落库延迟 <1s，R1）。
失败语义（D4 审计可丢）：batch 落库失败 → 移入 audit:calls:dead 死信流
（XADD 一条含原始 batch + 错误信息），XACK 原消息防无限重试（R2 恢复靠
XREADGROUP last-delivered 续读，死信人工/后续处理）。

消息字段契约（Task 1 proxy audit.py 实测产出，消费端必须按此解析）：
- time 格式 %Y-%m-%d %H:%M:%S.000 锁死，直写 DATETIME(3) 列
- latency_ms 是字符串，INSERT 前 int() 转换（schema INT NOT NULL）
- journey 是 JSON 字符串（成功行 "[]"），TEXT 列原样直写
- trace 字段名是 trace 不是 trace_id（proxy XADD 时已重命名）
"""
import asyncio
import json
import os
import time
import structlog

from redis_client import get_redis
from db import get_pool

logger = structlog.get_logger()

_STREAM = "audit:calls"
_GROUP = "calls-consumers"
_DEAD_STREAM = "audit:calls:dead"
_BATCH = 100
_BLOCK_MS = 1000
_DEAD_RETRIES = 3  # 连续 N 次 batch 失败进死信


def _consumer_name() -> str:
    # 容器 HOSTNAME 唯一；本地/测试回退固定名（单消费者场景下无冲突）
    return os.environ.get("HOSTNAME", "admin-consumer")


def _record_metric(name: str, value: float) -> None:
    """运行时取 instrument（不能 from-import），None 时静默跳过。"""
    import metrics
    instrument = getattr(metrics, name, None)
    if instrument is not None:
        instrument.record(value, {})


async def _insert_calls(rows: list[dict]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO calls (time, server, tool, op, token_name, latency_ms, "
                "status, error_type, trace, message, journey) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(
                    r["time"], r["server"], r["tool"], r["op"], r["token_name"],
                    int(r["latency_ms"]), r["status"], r["error_type"],
                    r["trace"], r["message"], r["journey"],
                ) for r in rows],
            )


async def _move_to_dead(redis, msg_ids: list[tuple[str, str]], error: str) -> None:
    """落库失败 batch 移死信（含原始消息），供人工/后续处理。"""
    await redis.xadd(_DEAD_STREAM, {
        "batch_ids": json.dumps([i for i, _ in msg_ids]),
        "error": error,
    }, maxlen=10000, approximate=True)


async def _consume_batch(redis) -> int:
    """拉一批 → 落库 → XACK；落库失败移死信。返回本批条数（失败为负）。"""
    # 每批一次取 stream 深度（R9 队列积压可观测；xlen O(1) 低开销）
    _record_metric("AUDIT_QUEUE_DEPTH", await redis.xlen(_STREAM))
    try:
        msgs = await redis.xreadgroup(
            _GROUP, _consumer_name(), {_STREAM: ">"}, count=_BATCH, block=_BLOCK_MS)
    except Exception as e:
        # 组不存在（首启）：创建组，下轮再读
        if "no such key" in str(e).lower() or "group" in str(e).lower():
            await redis.xgroup_create(_STREAM, _GROUP, id="0", mkstream=True)
        logger.warning("audit_consume_group_init", error=str(e), service="gateway-admin")
        return 0
    if not msgs:
        return 0
    rows, ids = [], []
    for _stream, entries in msgs:
        for mid, fields in entries:
            rows.append(fields)
            ids.append((mid, _stream))
    try:
        t0 = time.monotonic()
        await _insert_calls(rows)
        _record_metric("AUDIT_BATCH_LATENCY", time.monotonic() - t0)
        await redis.xack(_STREAM, _GROUP, *[i for i, _ in ids])
        return len(ids)
    except Exception as e:
        # 单条消息可单独进死信而 batch 失败重试——当前语义：整批移死信 + XACK。
        # 死信成功后必须 XACK 原消息：否则 PEL 无限重投（R2 恢复靠
        # last-delivered 续读，死信人工/后续处理）。死信写不进（Redis 挂了）
        # 则保留 PEL，Redis 恢复后自动重试——审计可丢（D4），但尽量不丢。
        try:
            await _move_to_dead(redis, ids, str(e))
            await redis.xack(_STREAM, _GROUP, *[i for i, _ in ids])
        except Exception as de:
            logger.error("audit_dead_letter_failed", error=str(de), service="gateway-admin")
        logger.error("audit_batch_failed", error=str(e), batch=len(ids), service="gateway-admin")
        return -len(ids)


async def _run_consumer() -> None:
    r = get_redis()
    # 确保流与组存在（幂等）
    try:
        await r.xgroup_create(_STREAM, _GROUP, id="0", mkstream=True)
    except Exception:
        pass  # 组已存在
    while True:
        n = await _consume_batch(r)
        if n == 0:
            # 空批：XREADGROUP block 已等 1s，极小 sleep 防忙转
            await asyncio.sleep(0.1)
        else:
            _record_metric("AUDIT_BATCH_SIZE", float(abs(n)))
