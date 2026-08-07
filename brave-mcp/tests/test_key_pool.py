"""KeyPool unit tests — rotation, failover, cooldown, hot reload."""
import json
import time
from unittest.mock import AsyncMock

import pytest
from key_pool import ErrorKind, KeyPool


def _rec(key_id, key, **over):
    base = {
        "key": key, "provider": "brave", "enabled": True,
        "monthly_quota": 2000, "status": "active",
        "cooldown_until": None, "remaining": None, "last_error": None,
    }
    base.update(over)
    return json.dumps(base)


class _FakePipeline:
    """redis-py pipeline 兼容替身：命令同步入队，execute 批量执行。

    on_success 的 hset+zadd+expire 三连经 pipeline 一次往返（spec 3.3）；
    execute 时逐个执行到 FakeRedis（复用 hset_calls/zadd_calls/expire_calls
    记录，现有断言不受影响），并递增 pipeline_executes 供计数断言。
    """

    def __init__(self, redis: "FakeRedis"):
        self._redis = redis
        self._cmds = []

    def hset(self, name, key=None, value=None, mapping=None):
        if mapping is None:
            mapping = {key: value}
        self._cmds.append(("hset", name, mapping))
        return self

    def zadd(self, name, mapping):
        self._cmds.append(("zadd", name, mapping))
        return self

    def expire(self, name, seconds):
        self._cmds.append(("expire", name, seconds))
        return self

    async def execute(self):
        self._redis.pipeline_executes += 1
        for kind, *args in self._cmds:
            if kind == "hset":
                # 镜像真实 redis-py：hset 的 mapping 是命名参数（位置传
                # 会给 key 参数——FakeRedis.hset 的 key 要求 str，dict
                # 传入即 unhashable）
                name, mapping = args
                await self._redis.hset(name, mapping=mapping)
            elif kind == "zadd":
                await self._redis.zadd(*args)
            elif kind == "expire":
                await self._redis.expire(*args)
        self._cmds.clear()
        return []


class FakeRedis:
    """Minimal async Redis fake with the methods KeyPool uses.

    Key-space: _records 支持两种形态——
    - 扁平 {field: value}（brief 测试预置/reload 测试直接写入）
    - 按 hash 名隔离 {hash_name: {field: value}}（hset 写回的真实形态）
    hgetall(name) 优先返回 name 名下字段；无则退回扁平形态，
    与真实 Redis 的 hash 语义一致（各 key 空间互不干扰）。
    """

    def __init__(self, records: dict[str, str]):
        self._records = dict(records)
        self.hset_calls = []
        self.zadd_calls = []
        self.expire_calls = []
        self.pipeline_executes = 0

    def pipeline(self):
        return _FakePipeline(self)

    def _fields_of(self, name: str) -> dict:
        owned = self._records.get(name)
        if isinstance(owned, dict):
            # 专属空间（hset 写回）优先；扁平预置的其余字段合并进来，
            # 保证「预置 k1/k2 + 写回」混合场景 reload 后字段完整
            merged = dict(self._records)
            merged.pop(name, None)
            merged.update(owned)
            return merged
        return self._records  # 扁平预置形态：name 无专属空间

    async def hgetall(self, name):
        return dict(self._fields_of(name))

    async def hset(self, name, key=None, value=None, mapping=None):
        # 镜像 redis.asyncio.Redis.hset：支持 hset(name, key, value)
        # 与 hset(name, mapping={...}) 两种调用形态；按 field 合并写入
        # 哈希（同 field 覆盖、异 field 保留），值序列化为 JSON 字符串
        # ——与真实 Redis（decode_responses=True 返回字符串）一致
        if mapping is None:
            mapping = {key: value}
        existing = self._records.get(name)
        if not isinstance(existing, dict):
            existing = {}
            self._records[name] = existing
        for field, val in mapping.items():
            existing[field] = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        self.hset_calls.append((name, mapping))
        return 1

    async def zadd(self, name, mapping):
        self.zadd_calls.append((name, mapping))
        return 1

    async def expire(self, name, seconds):
        self.expire_calls.append((name, seconds))
        return True


@pytest.fixture
async def pool():
    records = {
        "k1": _rec("k1", "BSA-a", status="active", remaining=900),
        "k2": _rec("k2", "BSA-b", status="active", remaining=800),
    }
    fake_redis = FakeRedis(records)
    pubsub = AsyncMock()
    pool = KeyPool("brave", fake_redis, pubsub, quota_default=2000)
    await pool.reload()
    return pool, fake_redis


async def test_next_key_prefers_higher_remaining(pool):
    pool_, _ = pool
    key = await pool_.next_key()
    assert key is not None
    assert key["key"] == "BSA-a"


async def test_next_key_borrow_spreads(pool):
    """2 个健康 key 时并发借 2 次，分散到不同 key（借用语义防 429 风暴）。

    brief 用例：2 key 同 remaining，第一次借用未归还时，第二次必须选另
    一个——in-flight 扣减参与挑选排序，同 remaining 的两 key 不会被打成
    同一个（借 1 次扣 1：同配额下只有借用能打破平局）。
    """
    pool_, _ = pool
    for r in pool_._records.values():
        r["remaining"] = 100  # 平局——挑选只能靠 in-flight 打破
    r1 = await pool_.next_key()
    r2 = await pool_.next_key()  # 第一次借用未归还
    assert r1["key_id"] != r2["key_id"]


async def test_next_key_borrow_released_on_success(pool):
    """归还语义：借用 → on_success 归还 → 再借可回到原 key。

    next_key 不归还 in-flight（借用期间计数保持，防并发扎堆同一 key）；
    成功记账时才归还。归还后挑选恢复按 remaining 排序，k1（900）优先。
    """
    pool_, _ = pool
    r1 = await pool_.next_key()
    assert r1["key_id"] == "k1"
    await pool_.on_success("k1")
    r2 = await pool_.next_key()
    assert r2["key_id"] == "k1"  # 归还后可回到原 key


async def test_next_key_borrow_released_on_error(pool):
    """失败路径同样归还：on_error 后 in-flight 清空，可再次借出。"""
    pool_, _ = pool
    r1 = await pool_.next_key()
    assert r1["key_id"] == "k1"
    await pool_.on_error("k1", ErrorKind.RATE_LIMIT)
    # RATE_LIMIT → cooldown，k2 健康且无借用 → 借 k2；两次借用后 in-flight
    # 不会残留负数或误扣（k2 借出即计数，归还计数由 on_error(k2) 完成）
    r2 = await pool_.next_key()
    assert r2["key_id"] == "k2"


async def test_borrow_after_reload_survives(pool):
    """reload 竞态：next_key 借用后 reload（换新 _records dict）→
    on_success 归还不炸、状态写回不丢（spec 3.2：reload 整表替换在锁内，
    记账持锁防旧 rec 写回覆盖新状态）。

    锁粒度核实：next_key 返回的 rec 是旧 dict 的引用；reload 换新 dict
    后 on_success 里 self._records.get(key_id) 取到的是新 rec（非旧引用），
    更新+写回落在新 rec 上——旧引用字段变更不再污染池状态。
    """
    pool_, fake_redis = pool
    borrowed = await pool_.next_key()
    assert borrowed["key_id"] == "k1"
    fake_redis._records["k3"] = _rec("k3", "BSA-c")
    await pool_.reload()  # 整表替换
    # 借用未归还即 reload：in-flight 计数在锁内调整，reload 不应清空它
    await pool_.on_success("k1", remaining=850)
    assert pool_._records["k1"]["remaining"] == 850
    assert pool_._records["k1"]["last_used_at"] is not None
    # 未归还的 in-flight 不泄漏成负数：再借一次 k1（无竞争时按 remaining 最高）
    r2 = await pool_.next_key()
    assert r2["key_id"] == "k1"


async def test_release_returns_borrow_without_writing_state(pool):
    """瞬时错误路径（classify_error 返回 None、不记账）也必须归还借用。

    不写 key 状态是「瞬时问题不记账」设计（超时不是 key 的问题）；但借用
    不归还会在每次超时泄漏 +1、key 被无限压低。release 只归还 in-flight，
    不改 records（last_error/status/cooldown 一律不碰）。
    """
    pool_, _ = pool
    r1 = await pool_.next_key()
    assert r1["key_id"] == "k1"
    before = dict(pool_._records["k1"])
    await pool_.release("k1")
    assert pool_._records["k1"] == before  # 状态零变更
    r2 = await pool_.next_key()
    assert r2["key_id"] == "k1"  # 归还后可回到原 key


async def test_on_success_redis_calls_via_pipeline(pool):
    """pipeline 化（spec 3.3）：on_success 的 hset+zadd+expire 三连合并为
    一次 Redis 往返。断言三命令仍全部执行（计数记录不退化），且只走了
    一次 pipeline.execute（三次直连调用会各记一次 pipeline_executes）。
    """
    pool_, fake_redis = pool
    await pool_.on_success("k1", remaining=890)
    # 三命令内容仍落库（FakePipeline.execute 委托到 FakeRedis 单命令）
    assert any(name == pool_._pool_key for name, _ in fake_redis.hset_calls)
    assert fake_redis.zadd_calls
    assert fake_redis.expire_calls
    assert fake_redis.pipeline_executes == 1  # 一次往返替代三次


async def test_next_key_skips_cooldown(pool):
    pool_, _ = pool
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 300))
    pool_._records["k1"]["status"] = "cooldown"
    pool_._records["k1"]["cooldown_until"] = future
    key = await pool_.next_key()
    assert key["key"] == "BSA-b"


async def test_next_key_returns_none_when_all_unavailable(pool):
    pool_, _ = pool
    for r in pool_._records.values():
        r["status"] = "invalid"
    assert await pool_.next_key() is None


async def test_on_error_invalid_marks_invalid(pool):
    pool_, _ = pool
    await pool_.on_error("k1", ErrorKind.INVALID)
    assert pool_._records["k1"]["status"] == "invalid"


async def test_on_error_rate_limit_sets_cooldown(pool):
    pool_, _ = pool
    await pool_.on_error("k1", ErrorKind.RATE_LIMIT)
    assert pool_._records["k1"]["status"] == "cooldown"
    assert pool_._records["k1"]["cooldown_until"] is not None


async def test_on_error_exhausted_sets_zero_remaining(pool):
    pool_, _ = pool
    await pool_.on_error("k1", ErrorKind.EXHAUSTED)
    assert pool_._records["k1"]["status"] == "exhausted"
    assert pool_._records["k1"]["remaining"] == 0


async def test_low_quota_skipped_but_fallback_used(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "low_quota"
    pool_._records["k1"]["remaining"] = 40
    pool_._records["k2"]["status"] = "invalid"
    key = await pool_.next_key()
    assert key["key"] == "BSA-a"  # fallback: only low_quota left


async def test_low_quota_skipped_when_healthy_others_exist(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "low_quota"
    pool_._records["k1"]["remaining"] = 40
    key = await pool_.next_key()
    assert key["key"] == "BSA-b"


async def test_low_quota_warning_participates(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "low_quota_warning"
    pool_._records["k1"]["remaining"] = 80
    key = await pool_.next_key()
    assert key is not None


async def test_unknown_quota_does_not_trigger_low(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "active"
    pool_._records["k1"]["remaining"] = None
    pool_._records["k2"]["status"] = "invalid"
    key = await pool_.next_key()
    assert key["key"] == "BSA-a"  # unknown → treated normal


async def test_on_success_records_usage_and_resets(pool):
    pool_, _ = pool
    await pool_.on_success("k1", remaining=890)
    assert pool_._records["k1"]["status"] == "active"
    assert pool_._records["k1"]["cooldown_until"] is None
    assert pool_._records["k1"]["remaining"] == 890
    assert pool_._records["k1"]["last_used_at"] is not None


async def test_on_success_preserves_low_quota_warning(pool):
    """成功不清低配额告警：ratio 8%（<10%）→ status 仍为 low_quota_warning。

    bug 回归：原实现无条件置 active 并写回 Redis，前台 API Keys 页读
    Redis status 展示告警时永远看不到低配额标红（spec「可用量 <10%
    前台明显提示」失效）。"""
    pool_, _ = pool
    pool_._records["k1"]["monthly_quota"] = 1000
    await pool_.on_success("k1", remaining=80)
    assert pool_._records["k1"]["status"] == "low_quota_warning"


async def test_on_success_preserves_low_quota(pool):
    """成功不清低配额状态：ratio 3%（<5%）→ status 仍为 low_quota。"""
    pool_, _ = pool
    pool_._records["k1"]["monthly_quota"] = 1000
    await pool_.on_success("k1", remaining=30)
    assert pool_._records["k1"]["status"] == "low_quota"


async def test_on_success_normal_quota_active(pool):
    """配额充足时成功仍置 active（回归：正常路径不受影响）。"""
    pool_, _ = pool
    pool_._records["k1"]["monthly_quota"] = 1000
    await pool_.on_success("k1", remaining=900)
    assert pool_._records["k1"]["status"] == "active"


async def test_reload_refreshes_records(pool):
    pool_, fake_redis = pool
    fake_redis._records["k3"] = _rec("k3", "BSA-c")
    await pool_.reload()
    assert "k3" in pool_._records


async def test_on_error_writes_back_to_redis(pool):
    """错误状态必须持久化：写回 Redis 后 reload 仍能看到新状态。"""
    pool_, fake_redis = pool
    await pool_.on_error("k1", ErrorKind.INVALID)
    assert any(name == pool_._pool_key for name, _ in fake_redis.hset_calls)
    await pool_.reload()
    assert pool_._records["k1"]["status"] == "invalid"


async def test_on_error_invalid_survives_reload(pool):
    """Redis 往返：reload 后 next_key 仍跳过 invalid 的 key。"""
    pool_, _ = pool
    await pool_.on_error("k1", ErrorKind.INVALID)
    await pool_.reload()
    key = await pool_.next_key()
    assert key is not None and key["key"] == "BSA-b"


async def test_on_success_writes_back_to_redis(pool):
    """成功路径同样持久化 remaining/status 回 Redis。"""
    pool_, fake_redis = pool
    await pool_.on_success("k1", remaining=890)
    assert any(name == pool_._pool_key for name, _ in fake_redis.hset_calls)
    await pool_.reload()
    assert pool_._records["k1"]["remaining"] == 890
    assert pool_._records["k1"]["status"] == "active"


async def test_listen_survives_pubsub_error(pool):
    """pubsub 故障不崩溃：get_message 抛异常后监听循环继续（I-1 回归）。"""
    import asyncio

    pool_, _ = pool

    class FlakyPubSub:
        def __init__(self):
            self.calls = 0

        # 签名与真实 redis-py 8.1.0 一致（ignore_subscribe_messages，
        # 无旧名参数——回归点与 test_listen_ignores_... 相同）
        async def get_message(self, ignore_subscribe_messages=False, timeout=30):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("redis down")
            return None  # 无消息 → 循环继续

    pool_._pubsub = FlakyPubSub()
    await pool_.start()
    await asyncio.sleep(0.2)
    # 第一次调用抛异常后监听循环必须仍存活（原 bug：NameError 使任务
    # 崩溃完成）；0.2s 内第二次调用（5s 后）尚未发生，故不数 calls
    assert not pool_._listen_task.done()
    assert pool_._pubsub.calls == 1
    pool_._listen_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pool_._listen_task


async def test_listen_resubscribes_after_pubsub_error(pool, monkeypatch):
    """pubsub 断线自愈（正式环境回归）：get_message 抛 ConnectionError
    后必须重建订阅——原实现只 sleep 重试，redis-py 的 pubsub 连接死后
    get_message 永远抛错（不会自动重连），热更新永久失效只能重启容器。
    断言重建（redis.pubsub() 工厂 + 新对象 subscribe 被调）与重建后
    消息触发 reload（新 key 可见）。"""
    import asyncio

    pool_, fake_redis = pool
    fake_redis._records["k3"] = _rec("k3", "BSA-c")

    # 加速重试循环：把 _listen 的 5s sleep 压成一次调度让出，避免测试
    # 等真实 5 秒。保留原 sleep 引用供自身让出控制权——打补丁后不能再
    # 调 asyncio.sleep（会无限递归）
    orig_sleep = asyncio.sleep

    async def fast_sleep(_):
        await orig_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    raised_once = {"done": False}

    class ReconnectingPubSub:
        """首次 get_message 抛 ConnectionError（连接已死），此后返回
        消息——模拟 Redis 重启后恢复。subscribe 记录频道供断言。"""

        def __init__(self):
            self.calls = 0
            self.subscribed_channels = None

        async def subscribe(self, *channels, **kwargs):
            self.subscribed_channels = channels

        async def get_message(self, ignore_subscribe_messages=False, timeout=30):
            await orig_sleep(0)
            self.calls += 1
            if not raised_once["done"]:
                raised_once["done"] = True
                raise ConnectionError("redis down")
            return {"type": "message", "channel": b"search:keys:channel",
                    "data": b"reload"}

    pubsub_factory_calls = {"n": 0}

    def make_pubsub():
        # 重建必须走 redis client 工厂（连接池每次给新 pubsub 对象）
        pubsub_factory_calls["n"] += 1
        return ReconnectingPubSub()

    pool_._pubsub = ReconnectingPubSub()  # 初始 pubsub：首次调用即抛错
    fake_redis.pubsub = make_pubsub       # 连接死后从工厂拿新对象
    await pool_.start()
    for _ in range(20):
        await orig_sleep(0)  # 多次让出，让监听循环走完 重建→重试 全过程
    # 断线后必须重建订阅：redis.pubsub() 工厂与新对象 subscribe 都被调用
    assert pubsub_factory_calls["n"] == 1
    assert pool_._pubsub.subscribed_channels == ("search:keys:channel",)
    # 重建后的订阅能收到消息 → reload 生效，新 key 可见（原 bug 下只有
    # key_pool_listen_retry 循环，新 key 永远不可见）
    assert "k3" in pool_._records
    assert not pool_._listen_task.done()
    pool_._listen_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pool_._listen_task


async def test_listen_ignores_subscribe_confirm_and_reloads_on_message(pool):
    """热更新消息接收回归：subscribe 确认消息必须被滤掉、message 触发
    reload。曾因 get_message(ignore_subscribe=...) 参数名在 redis-py 6+
    改名导致每次调用必 TypeError（监听静默失效），修复后不得回归。"""
    import asyncio

    pool_, fake_redis = pool
    fake_redis._records["k3"] = _rec("k3", "BSA-c")

    messages = [
        # 模拟订阅确认消息（redis-py 8 返回 "subscribe" type）
        {"type": "subscribe", "channel": b"search:keys:channel", "data": 1},
        {"type": "message", "channel": b"search:keys:channel",
         "data": b"reload"},
        None,  # 无消息 → 循环继续
    ]

    class ScriptedPubSub:
        def __init__(self):
            self.calls = 0

        # 签名必须与真实 redis-py 8.1.0 一致（ignore_subscribe_messages，
        # 无旧名参数——回归点：旧实现 `ignore_subscribe=True` 在此签名
        # 下直接 TypeError，与线上 redis-py 8 表现一致）
        async def get_message(self, ignore_subscribe_messages=False, timeout=30):
            # 必须让出控制权：无挂起点的协程立即返回会让 _listen 变成
            # CPU 忙循环，测试的 sleep 永远得不到调度（实测挂起）
            await asyncio.sleep(0)
            self.calls += 1
            return messages[self.calls - 1] if self.calls <= len(messages) else None

    pool_._pubsub = ScriptedPubSub()
    await pool_.start()
    await asyncio.sleep(0.2)
    assert not pool_._listen_task.done()  # 监听循环存活（无 TypeError 循环退出）
    # subscribe 确认被滤掉 → 不触发 reload；message 触发 reload → k3 可见
    assert "k3" in pool_._records
    assert pool_._pubsub.calls >= 2
    pool_._listen_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pool_._listen_task
