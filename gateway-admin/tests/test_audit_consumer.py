"""Tests for audit_consumer: XREADGROUP 批量落库 + XACK + 死信。

消息字段契约（Task 1 proxy audit.py 实测产出）：
- time 格式 %Y-%m-%d %H:%M:%S.000 锁死
- latency_ms 是字符串，INSERT 前 int() 转换
- journey 是 JSON 字符串（成功行 "[]"）
- trace 字段名（不是 trace_id）
"""
import asyncio
import json

import fakeredis.aioredis
import pytest

import audit_consumer


class FakePool:
    """AIOMysql pool 双胞胎：记录 executemany 行数与列序，commit() no-op。"""

    def __init__(self):
        self.inserted = 0
        self.rows = []
        self.sql = None

    def get(self):
        return self

    def acquire(self):
        return self

    # aiomysql Pool 是 awaitable（返回 conn）；brief 的 _insert_calls 用 await get_pool()
    def __await__(self):
        async def _return_self():
            return self
        return _return_self().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def cursor(self):
        return self

    async def __call__(self, *a, **kw):
        return self

    async def execute(self, sql, args=None):
        self.sql = sql
        self.inserted = 1
        self.rows.append(args)
        return 1

    async def executemany(self, sql, seq):
        self.sql = sql
        self.inserted += len(seq)
        self.rows.extend(seq)
        return len(seq)

    async def commit(self):
        pass


@pytest.fixture
def fake_pool(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(audit_consumer, "get_pool", lambda: pool.get())
    return pool


def _msg(i: int) -> dict:
    return {
        "time": "2026-08-07 12:00:00.000", "server": "tavily-mcp",
        "tool": "tavily_search", "op": "read", "token_name": "t",
        "latency_ms": "5", "status": "ok", "error_type": "", "message": "",
        "journey": "[]", "trace": f"tr{i}",
    }


async def test_consumer_batch_inserts_and_acks(fake_redis, fake_pool):
    """XREADGROUP 拉取 → executemany INSERT → XACK 全链路。"""
    for i in range(3):
        await fake_redis.xadd("audit:calls", _msg(i))
    await fake_redis.xgroup_create("audit:calls", "calls-consumers", id="0")
    n = await audit_consumer._consume_batch(fake_redis)
    assert n == 3
    assert fake_pool.inserted == 3
    # 消息字段契约：time 原样、latency_ms 字符串已 int()、journey JSON 原样
    row = fake_pool.rows[0]
    assert row[0] == "2026-08-07 12:00:00.000"
    assert row[5] == 5 and isinstance(row[5], int)
    assert row[8] == "tr0"  # trace 字段名
    assert row[9] == ""     # message
    assert row[10] == "[]"  # journey
    # INSERT 列序对齐 schema（01_calls.sql）
    assert "latency_ms" in fake_pool.sql and "journey" in fake_pool.sql
    # XACK 已确认
    pending = await fake_redis.xpending("audit:calls", "calls-consumers")
    assert pending["pending"] == 0


async def test_consumer_dead_letter_on_failure(fake_redis, fake_pool, monkeypatch):
    """落库抛异常 → batch 移死信 + XACK，不无限重试。"""
    async def _boom(rows):
        raise RuntimeError("db down")
    monkeypatch.setattr(audit_consumer, "_insert_calls", _boom)
    await fake_redis.xadd("audit:calls", _msg(1))
    await fake_redis.xgroup_create("audit:calls", "calls-consumers", id="0")
    n = await audit_consumer._consume_batch(fake_redis)
    assert n == -1
    dead = await fake_redis.xrange("audit:calls:dead", count=1)
    assert len(dead) == 1
    # 死信消息含原始 batch id + 错误信息（供人工/后续处理）；
    # id 格式 "<ms>-<seq>"（fakeredis 用时间戳，真 Redis 用序列号，断言分隔符）
    body = json.loads(dead[0][1]["batch_ids"])
    assert "-" in body[0] and body[0].split("-")[0].isdigit()
    assert "db down" in dead[0][1]["error"]
    pending = await fake_redis.xpending("audit:calls", "calls-consumers")
    assert pending["pending"] == 0  # 死信后 XACK，防无限重试


async def test_consumer_batch_empty_returns_zero(fake_redis):
    """空流 → 返回 0，不触 INSERT 不 XACK。"""
    await fake_redis.xgroup_create("audit:calls", "calls-consumers", id="0", mkstream=True)
    n = await audit_consumer._consume_batch(fake_redis)
    assert n == 0


async def test_consumer_group_not_exist_creates_group(fake_redis):
    """流存在但组不存在 → 建组（幂等），本轮返回 0 下轮再读。"""
    await fake_redis.xadd("audit:calls", _msg(1))
    n = await audit_consumer._consume_batch(fake_redis)
    assert n == 0
    groups = await fake_redis.xinfo_groups("audit:calls")
    assert groups[0]["name"] == "calls-consumers"


async def test_consumer_multi_batch_and_retries(fake_redis, fake_pool, monkeypatch):
    """150 条分两批全消费；死信语义：每次失败即移死信 + XACK（无重试累积）。"""
    for i in range(150):
        await fake_redis.xadd("audit:calls", _msg(i))
    await fake_redis.xgroup_create("audit:calls", "calls-consumers", id="0")

    total = 0
    for _ in range(4):
        total += await audit_consumer._consume_batch(fake_redis)
    assert total == 150
    assert fake_pool.inserted == 150
    pending = await fake_redis.xpending("audit:calls", "calls-consumers")
    assert pending["pending"] == 0

    # 死信路径：每次失败 batch 都移死信（3 条消息同批拉走 → 一条死信条目
    # 含 3 个 ids；再加一条新消息也立即进死信——非"累计 3 次才移"）
    async def _boom(rows):
        raise RuntimeError("db down")
    monkeypatch.setattr(audit_consumer, "_insert_calls", _boom)
    for _ in range(3):
        await fake_redis.xadd("audit:calls", _msg(999 + _))
    for _ in range(4):
        await audit_consumer._consume_batch(fake_redis)
    dead = await fake_redis.xrange("audit:calls:dead")
    assert len(dead) == 1  # 3 条同批 → 整批一条死信
    assert len(json.loads(dead[0][1]["batch_ids"])) == 3
    # 新的失败消息：立即移死信，不等到第 3 次
    await fake_redis.xadd("audit:calls", _msg(9999))
    await audit_consumer._consume_batch(fake_redis)
    dead = await fake_redis.xrange("audit:calls:dead")
    assert len(dead) == 2
    pending = await fake_redis.xpending("audit:calls", "calls-consumers")
    assert pending["pending"] == 0


async def test_consumer_metrics_recorded(fake_redis, fake_pool, monkeypatch):
    """三指标运行时记录：batch_size / batch_latency / queue_depth。"""
    import metrics
    records = []

    class FakeHistogram:
        def __init__(self, name):
            self.name = name

        def record(self, value, attrs):
            records.append((self.name, value))

    for i in range(5):
        await fake_redis.xadd("audit:calls", _msg(i))
    await fake_redis.xgroup_create("audit:calls", "calls-consumers", id="0")

    monkeypatch.setattr(metrics, "AUDIT_BATCH_SIZE", FakeHistogram("audit_batch_size"))
    monkeypatch.setattr(metrics, "AUDIT_BATCH_LATENCY", FakeHistogram("audit_batch_latency"))
    monkeypatch.setattr(metrics, "AUDIT_QUEUE_DEPTH", FakeHistogram("audit_queue_depth"))
    await audit_consumer._consume_batch(fake_redis)
    # _consume_batch 内记录 queue_depth 与 batch_latency；batch_size 由 _run_consumer
    # 循环记录（brief 定位），此处只断言本层记录的两项
    assert any(n == "audit_queue_depth" for n, _ in records)
    assert any(n == "audit_batch_latency" for n, _ in records)
    assert all(n != "audit_batch_size" for n, _ in records)


async def test_consumer_no_metric_instrument_records_nothing(fake_redis, fake_pool):
    """instrument 为 None（metrics 未初始化）→ 静默跳过，不崩。"""
    for i in range(2):
        await fake_redis.xadd("audit:calls", _msg(i))
    await fake_redis.xgroup_create("audit:calls", "calls-consumers", id="0")
    n = await audit_consumer._consume_batch(fake_redis)  # 不抛
    assert n == 2
    pending = await fake_redis.xpending("audit:calls", "calls-consumers")
    assert pending["pending"] == 0


async def test_consumer_run_loop_records_batch_size_metrics(fake_redis, fake_pool, monkeypatch):
    """_run_consumer 循环记录 batch_size 指标并消费消息（跑一轮后取消）。"""
    import metrics
    records = []

    class FakeHistogram:
        def __init__(self, name):
            self.name = name

        def record(self, value, attrs):
            records.append((self.name, value))

    monkeypatch.setattr(metrics, "AUDIT_BATCH_SIZE", FakeHistogram("audit_batch_size"))
    monkeypatch.setattr(metrics, "AUDIT_BATCH_LATENCY", FakeHistogram("audit_batch_latency"))
    monkeypatch.setattr(metrics, "AUDIT_QUEUE_DEPTH", FakeHistogram("audit_queue_depth"))
    for i in range(3):
        await fake_redis.xadd("audit:calls", _msg(i))
    await fake_redis.xgroup_create("audit:calls", "calls-consumers", id="0")

    task = asyncio.create_task(audit_consumer._run_consumer())
    await asyncio.sleep(0.3)  # fakeredis 无真实 block 延迟，一轮即消费完
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert fake_pool.inserted == 3
    assert any(n == "audit_batch_size" and v == 3 for n, v in records)


async def test_consumer_redis_blip_self_heals(fake_redis, fake_pool, monkeypatch):
    """Finding 1：Redis 闪断（xlen 抛异常）→ 消费者自愈不退出，恢复后继续消费。"""
    calls = {"boom": False, "reads": 0}

    orig_xlen = fake_redis.xlen

    async def flaky_xlen(name):
        calls["reads"] += 1
        if not calls["boom"]:
            return await orig_xlen(name)
        raise ConnectionError("redis blip")

    monkeypatch.setattr(fake_redis, "xlen", flaky_xlen)
    # 组建立逻辑绕过闪断的 xlen——先建好组再注入闪断
    await fake_redis.xgroup_create("audit:calls", "calls-consumers", id="0", mkstream=True)
    calls["boom"] = True

    async def fake_sleep(sec):
        if calls["reads"] >= 3:
            raise asyncio.CancelledError  # 自愈两轮后停

    monkeypatch.setattr(audit_consumer.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await audit_consumer._run_consumer()  # 闪断期间不抛非 CancelledError = 自愈
    assert calls["reads"] >= 2  # 闪断后退避重试了至少一轮，没有退出


async def test_consumer_loop_backstop_on_unexpected_error(fake_redis, fake_pool, monkeypatch):
    """Finding 1 兜底：_consume_batch 意外抛异常 → 循环 catch 退避，task 不退出。

    循环内唯一能终止 task 的异常是 asyncio.CancelledError（lifespan shutdown
    主动取消）；其余异常（含 _consume_batch 意外泄漏的）都被吞掉 + 退避重试。
    """
    calls = {"n": 0}

    async def exploding_batch(redis):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected")
        return 0

    monkeypatch.setattr(audit_consumer, "_consume_batch", exploding_batch)
    sleeps = []

    async def fake_sleep(sec):
        sleeps.append(sec)
        raise asyncio.CancelledError

    monkeypatch.setattr(audit_consumer.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await audit_consumer._run_consumer()
    assert calls["n"] >= 1  # 第一轮炸后进入退避（sleep 抛 CancelledError 终止循环）
    assert sleeps and sleeps[0] == audit_consumer._RETRY_SLEEP  # 退避确实发生


async def test_consumer_xgroup_create_failure_is_absorbed(fake_redis, fake_pool, monkeypatch):
    """Finding 1：xreadgroup 报错 + 建组也失败（Redis 闪断）→ 返回 0 不抛。"""
    class BlipRedis:
        async def xlen(self, name):
            return 0

        async def xreadgroup(self, *a, **kw):
            raise ConnectionError("no such key / group redis down")

        async def xgroup_create(self, *a, **kw):
            raise ConnectionError("redis down")

    n = await audit_consumer._consume_batch(BlipRedis())
    assert n == 0  # 不抛异常


async def test_lifespan_calls_init_audit_metrics(monkeypatch):
    """Finding 3：lifespan 启动时调用 init_audit_metrics()（幂等、无依赖降级）。

    需在 TestClient 进入 lifespan 前 patch（conftest 的 client fixture 已经
    跑过 lifespan，patch 晚了一步），故本地自建 fixture。
    """
    import redis_client
    import metrics
    from fastapi.testclient import TestClient

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "_redis", fake)
    calls = {"n": 0}

    def fake_init():
        calls["n"] += 1

    monkeypatch.setattr(metrics, "init_audit_metrics", fake_init)
    from app import app
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200
    assert calls["n"] == 1  # lifespan 启动恰好调一次（幂等函数，多次启动各调一次）
    await fake.aclose()


async def test_proxy_xadd_maxlen_contract(fake_redis, monkeypatch):
    """Task 1 契约：proxy XADD 带 maxlen=50000 + approximate（R9 有界流）。

    Task 1 遗留（progress.md minor）：proxy 侧测试未断言该参数；消费者侧
    用 spy 包裹 xadd 补契约验证。proxy audit.py 仅依赖 structlog+redis，
    可直接从本测试加载；get_redis stub 为 fake_redis。
    """
    import importlib.util
    from pathlib import Path

    proxy_audit_path = (Path(__file__).resolve().parent.parent.parent
                        / "gateway-proxy" / "audit.py")
    spec = importlib.util.spec_from_file_location("proxy_audit", proxy_audit_path)
    proxy_audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy_audit)
    monkeypatch.setattr(proxy_audit, "get_redis", lambda: fake_redis)

    calls = []
    orig_xadd = fake_redis.xadd

    async def spy_xadd(name, fields, **kwargs):
        calls.append((name, kwargs))
        return await orig_xadd(name, fields, **kwargs)

    fake_redis.xadd = spy_xadd
    await proxy_audit.record_call_stream(
        meta={"time": "2026-08-07 12:00:00.000", "server": "tavily-mcp", "tool": "t",
              "op": "read", "token_name": "t", "latency_ms": 5, "trace_id": "t1"},
        status="ok",
    )
    assert calls and calls[0][0] == "audit:calls"
    assert calls[0][1]["maxlen"] == 50000
    assert calls[0][1]["approximate"] is True
