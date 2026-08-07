"""审计消费者：XREADGROUP audit:calls → executemany 批量 INSERT calls 表 → XACK。

D2：消费者放 gateway-admin（lifespan 后台 task），非 proxy 非独立容器。
批量参数：batch=100, block=1s（落库延迟 <1s，R1）。
失败语义（D4 审计可丢）：**每次** batch 落库失败即整批移入 audit:calls:dead
死信流（XADD 一条含原始 batch ids + 错误信息），并 XACK 原消息——无重试
累积：不 XACK 会让 PEL 无限重投。R2 恢复靠 XREADGROUP last-delivered 续读，
死信人工/后续处理。消费者单例必须自愈：循环内任何未预期异常（Redis 闪断）
记录后退避重试，绝不退出（task 崩溃 = 审计永久停摆）。

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
_RETRY_SLEEP = 1.0  # 未预期异常退避重试间隔（审计可丢 D4，恢复速度优先）


def _consumer_name() -> str:
    # 容器 HOSTNAME 唯一；本地/测试回退固定名（单消费者场景下无冲突）
    return os.environ.get("HOSTNAME", "admin-consumer")


def _record_metric(name: str, value: float) -> None:
    """运行时取 instrument（不能 from-import），None 时静默跳过。

    记录本身失败（OTel SDK 异常）也消化掉——指标绝不污染消费路径：
    成功路径里若此处抛异常，会把整批成功落库的消息误移死信。
    """
    try:
        import metrics
        instrument = getattr(metrics, name, None)
        if instrument is not None:
            instrument.record(value, {})
    except Exception as e:
        logger.warning("audit_metric_record_failed", metric=name, error=str(e),
                       service="gateway-admin")


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
    """拉一批 → 落库 → XACK；落库失败移死信。返回本批条数（失败为负）。

    整个函数（含 queue_depth 采集与建组）不抛异常——所有未预期情况
    （Redis 闪断等）在这里消化成日志 + 返回 0，让上层循环继续退避重试。
    """
    # 每批一次取 stream 深度（R9 队列积压可观测；xlen O(1) 低开销）
    try:
        _record_metric("AUDIT_QUEUE_DEPTH", await redis.xlen(_STREAM))
    except Exception as e:
        # Redis 闪断：本批跳过，返回 0 让 _run_consumer 退避后重试
        logger.warning("audit_consume_redis_unavailable", error=str(e), service="gateway-admin")
        return 0
    try:
        msgs = await redis.xreadgroup(
            _GROUP, _consumer_name(), {_STREAM: ">"}, count=_BATCH, block=_BLOCK_MS)
    except Exception as e:
        # 组不存在（首启）：创建组，下轮再读；建组自身失败（Redis 闪断）
        # 也消化掉，不抛——上层循环负责退避重试
        if "no such key" in str(e).lower() or "group" in str(e).lower():
            try:
                await redis.xgroup_create(_STREAM, _GROUP, id="0", mkstream=True)
            except Exception as gce:
                logger.warning("audit_consume_group_create_failed", error=str(gce),
                               service="gateway-admin")
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
        # 每次失败即死信 + XACK（保守语义，无重试累积）：死信成功后必须 XACK
        # 原消息，否则 PEL 无限重投。死信写失败时消息保留在 PEL，不会被 `>`
        # 重读，需 XAUTOCLAIM 人工恢复（D4 审计可丢容忍）。
        try:
            await _move_to_dead(redis, ids, str(e))
            await redis.xack(_STREAM, _GROUP, *[i for i, _ in ids])
        except Exception as de:
            logger.error("audit_dead_letter_failed", error=str(de), service="gateway-admin")
        logger.error("audit_batch_failed", error=str(e), batch=len(ids), service="gateway-admin")
        return -len(ids)


async def _run_consumer() -> None:
    r = get_redis()
    # 确保流与组存在（幂等）；失败不阻塞启动——_consume_batch 内会再试
    try:
        await r.xgroup_create(_STREAM, _GROUP, id="0", mkstream=True)
    except Exception:
        pass  # 组已存在
    while True:
        try:
            n = await _consume_batch(r)
        except Exception as e:
            # 兜底：任何未预期异常（_consume_batch 应已消化）都不能让消费者
            # task 退出——退避后继续（消费者单例必须自愈，task 崩溃 = 审计停摆）
            logger.error("audit_consumer_loop_crashed", error=str(e),
                         error_type=type(e).__name__, service="gateway-admin")
            await asyncio.sleep(_RETRY_SLEEP)
            continue
        if n == 0:
            # 空批：XREADGROUP block 已等 1s，极小 sleep 防忙转
            await asyncio.sleep(0.1)
        else:
            _record_metric("AUDIT_BATCH_SIZE", float(abs(n)))
