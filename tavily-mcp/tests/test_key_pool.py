"""KeyPool unit tests — rotation, failover, cooldown, hot reload."""
import json
import time
from unittest.mock import AsyncMock

import pytest
from key_pool import ErrorKind, KeyPool


def _rec(key_id, key, **over):
    base = {
        "key": key, "provider": "tavily", "enabled": True,
        "monthly_quota": 1000, "status": "active",
        "cooldown_until": None, "remaining": None, "last_error": None,
    }
    base.update(over)
    return json.dumps(base)


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
        "k1": _rec("k1", "tvly-a", status="active", remaining=900),
        "k2": _rec("k2", "tvly-b", status="active", remaining=800),
    }
    fake_redis = FakeRedis(records)
    pubsub = AsyncMock()
    pool = KeyPool("tavily", fake_redis, pubsub, quota_default=1000)
    await pool.reload()
    return pool, fake_redis


async def test_next_key_prefers_higher_remaining(pool):
    pool_, _ = pool
    key = await pool_.next_key()
    assert key is not None
    assert key["key"] == "tvly-a"


async def test_next_key_skips_cooldown(pool):
    pool_, _ = pool
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 300))
    pool_._records["k1"]["status"] = "cooldown"
    pool_._records["k1"]["cooldown_until"] = future
    key = await pool_.next_key()
    assert key["key"] == "tvly-b"


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
    assert key["key"] == "tvly-a"  # fallback: only low_quota left


async def test_low_quota_skipped_when_healthy_others_exist(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "low_quota"
    pool_._records["k1"]["remaining"] = 40
    key = await pool_.next_key()
    assert key["key"] == "tvly-b"


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
    assert key["key"] == "tvly-a"  # unknown → treated normal


async def test_on_success_records_usage_and_resets(pool):
    pool_, _ = pool
    await pool_.on_success("k1", remaining=890)
    assert pool_._records["k1"]["status"] == "active"
    assert pool_._records["k1"]["cooldown_until"] is None
    assert pool_._records["k1"]["remaining"] == 890
    assert pool_._records["k1"]["last_used_at"] is not None


async def test_reload_refreshes_records(pool):
    pool_, fake_redis = pool
    fake_redis._records["k3"] = _rec("k3", "tvly-c")
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
    assert key is not None and key["key"] == "tvly-b"


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

        async def get_message(self, ignore_subscribe=True, timeout=30):
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
