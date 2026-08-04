"""KeyPool unit tests — rotation, failover, cooldown, hot reload.

与 tavily/brave 同构（KeyPool 逻辑 provider 无关，原样复制）；
provider 维度为 "serpapi"、quota_default=100。
"""
import json
import time
from unittest.mock import AsyncMock

import pytest
from key_pool import ErrorKind, KeyPool


def _rec(key_id, key, **over):
    base = {
        "key": key, "provider": "serpapi", "enabled": True,
        "monthly_quota": 100, "status": "active",
        "cooldown_until": None, "remaining": None, "last_error": None,
    }
    base.update(over)
    return json.dumps(base)


class FakeRedis:
    """Minimal async Redis fake with the methods KeyPool uses.

    Key-space: _records 支持两种形态——
    - 扁平 {field: value}（reload 测试直接写入）
    - 按 hash 名隔离 {hash_name: {field: value}}（hset 写回的真实形态）
    hgetall(name) 优先返回 name 名下字段；无则退回扁平形态。
    """

    def __init__(self, records: dict[str, str]):
        self._records = dict(records)
        self.hset_calls = []
        self.zadd_calls = []
        self.expire_calls = []

    def _fields_of(self, name: str) -> dict:
        owned = self._records.get(name)
        if isinstance(owned, dict):
            merged = dict(self._records)
            merged.pop(name, None)
            merged.update(owned)
            return merged
        return self._records

    async def hgetall(self, name):
        return dict(self._fields_of(name))

    async def hset(self, name, key=None, value=None, mapping=None):
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
        "k1": _rec("k1", "SERP-a", status="active", remaining=90),
        "k2": _rec("k2", "SERP-b", status="active", remaining=80),
    }
    fake_redis = FakeRedis(records)
    pubsub = AsyncMock()
    pool = KeyPool("serpapi", fake_redis, pubsub, quota_default=100)
    await pool.reload()
    return pool, fake_redis


async def test_next_key_prefers_higher_remaining(pool):
    pool_, _ = pool
    key = await pool_.next_key()
    assert key is not None
    assert key["key"] == "SERP-a"


async def test_next_key_skips_cooldown(pool):
    pool_, _ = pool
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 300))
    pool_._records["k1"]["status"] = "cooldown"
    pool_._records["k1"]["cooldown_until"] = future
    key = await pool_.next_key()
    assert key["key"] == "SERP-b"


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
    pool_._records["k1"]["remaining"] = 4
    pool_._records["k2"]["status"] = "invalid"
    key = await pool_.next_key()
    assert key["key"] == "SERP-a"  # fallback: only low_quota left


async def test_low_quota_skipped_when_healthy_others_exist(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "low_quota"
    pool_._records["k1"]["remaining"] = 4
    key = await pool_.next_key()
    assert key["key"] == "SERP-b"


async def test_low_quota_warning_participates(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "low_quota_warning"
    pool_._records["k1"]["remaining"] = 8
    key = await pool_.next_key()
    assert key is not None


async def test_unknown_quota_does_not_trigger_low(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "active"
    pool_._records["k1"]["remaining"] = None
    pool_._records["k2"]["status"] = "invalid"
    key = await pool_.next_key()
    assert key["key"] == "SERP-a"  # unknown → treated normal


async def test_on_success_records_usage_and_resets(pool):
    pool_, _ = pool
    await pool_.on_success("k1", remaining=89)
    assert pool_._records["k1"]["status"] == "active"
    assert pool_._records["k1"]["cooldown_until"] is None
    assert pool_._records["k1"]["remaining"] == 89
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
    fake_redis._records["k3"] = _rec("k3", "SERP-c")
    await pool_.reload()
    assert "k3" in pool_._records


async def test_on_error_writes_back_to_redis(pool):
    pool_, fake_redis = pool
    await pool_.on_error("k1", ErrorKind.INVALID)
    assert any(name == pool_._pool_key for name, _ in fake_redis.hset_calls)
    await pool_.reload()
    assert pool_._records["k1"]["status"] == "invalid"


async def test_on_error_invalid_survives_reload(pool):
    pool_, _ = pool
    await pool_.on_error("k1", ErrorKind.INVALID)
    await pool_.reload()
    key = await pool_.next_key()
    assert key is not None and key["key"] == "SERP-b"


async def test_on_success_writes_back_to_redis(pool):
    pool_, fake_redis = pool
    await pool_.on_success("k1", remaining=89)
    assert any(name == pool_._pool_key for name, _ in fake_redis.hset_calls)
    await pool_.reload()
    assert pool_._records["k1"]["remaining"] == 89
    assert pool_._records["k1"]["status"] == "active"


async def test_listen_survives_pubsub_error(pool):
    """pubsub 故障不崩溃：get_message 抛异常后监听循环继续。"""
    import asyncio

    pool_, _ = pool

    class FlakyPubSub:
        def __init__(self):
            self.calls = 0

        # 签名与真实 redis-py 8.1.0 一致（ignore_subscribe_messages，
        # 无旧名参数——回归点：旧实现 ignore_subscribe=True 直接 TypeError）
        async def get_message(self, ignore_subscribe_messages=False, timeout=30):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("redis down")
            return None

    pool_._pubsub = FlakyPubSub()
    await pool_.start()
    await asyncio.sleep(0.2)
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
    fake_redis._records["k3"] = _rec("k3", "SERP-c")

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
    reload（redis-py 6+ 参数改名踩坑点，见 brave-mcp 注释）。"""
    import asyncio

    pool_, fake_redis = pool
    fake_redis._records["k3"] = _rec("k3", "SERP-c")

    messages = [
        {"type": "subscribe", "channel": b"search:keys:channel", "data": 1},
        {"type": "message", "channel": b"search:keys:channel", "data": b"reload"},
        None,
    ]

    class ScriptedPubSub:
        def __init__(self):
            self.calls = 0

        async def get_message(self, ignore_subscribe_messages=False, timeout=30):
            # 必须让出控制权：无挂起点的协程立即返回会让 _listen 变成
            # CPU 忙循环，测试的 sleep 永远得不到调度
            await asyncio.sleep(0)
            self.calls += 1
            return messages[self.calls - 1] if self.calls <= len(messages) else None

    pool_._pubsub = ScriptedPubSub()
    await pool_.start()
    await asyncio.sleep(0.2)
    assert not pool_._listen_task.done()
    assert "k3" in pool_._records
    assert pool_._pubsub.calls >= 2
    pool_._listen_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pool_._listen_task
