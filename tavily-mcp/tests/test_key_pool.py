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
    """Minimal async Redis fake with the methods KeyPool uses."""

    def __init__(self, records: dict[str, str]):
        self._records = dict(records)
        self.hset_calls = []
        self.zadd_calls = []
        self.expire_calls = []

    async def hgetall(self, name):
        return dict(self._records)

    async def hset(self, name, key=None, value=None, mapping=None):
        # 镜像 redis.asyncio.Redis.hset：支持 hset(name, key, value)
        # 与 hset(name, mapping={...}) 两种调用形态
        if mapping is None:
            mapping = {key: value}
        self._records[name] = mapping
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
