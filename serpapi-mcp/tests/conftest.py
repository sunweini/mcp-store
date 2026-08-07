"""Shared httpx mock transport + pool fixture for serpapi tests."""
import json
import pytest
import httpx

from key_pool import KeyPool


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self._status_code = status_code
        self._headers = headers or {}
        self.last_request = None

    async def handle_async_request(self, request):
        self.last_request = request
        return httpx.Response(
            self._status_code, json=self._payload,
            headers=self._headers, request=request)


@pytest.fixture
async def fake_pool():
    """App-level KeyPool fixture: real KeyPool over FakeRedis, two keys.

    k1 剩余 900 > k2 剩余 800 —— next_key 默认返回 k1。工具层测试以此
    验证 pool 集成（成功/失败记账、key 失效后 failover 到 k2）。
    """
    records = {
        "k1": json.dumps({"key": "SERP-a", "provider": "serpapi", "enabled": True,
                          "monthly_quota": 100, "status": "active",
                          "cooldown_until": None, "remaining": 90, "last_error": None}),
        "k2": json.dumps({"key": "SERP-b", "provider": "serpapi", "enabled": True,
                          "monthly_quota": 100, "status": "active",
                          "cooldown_until": None, "remaining": 80, "last_error": None}),
    }

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

        def pipeline(self):
            # Task 6：on_success 的 hset+zadd+expire 三连走 pipeline 一次往返。
            # 委托到 FakeRedis 单命令（复用 hset_calls/zadd_calls/expire_calls）
            redis = self

            class _Pipe:
                def __init__(self):
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
                    for kind, *args in self._cmds:
                        if kind == "hset":
                            name, mapping = args
                            await redis.hset(name, mapping=mapping)
                        elif kind == "zadd":
                            await redis.zadd(*args)
                        elif kind == "expire":
                            await redis.expire(*args)
                    self._cmds.clear()
                    return []

            return _Pipe()

        async def zadd(self, name, mapping):
            self.zadd_calls.append((name, mapping))
            return 1

        async def expire(self, name, seconds):
            self.expire_calls.append((name, seconds))
            return True

    from unittest.mock import AsyncMock
    fake_redis = FakeRedis(records)
    pool = KeyPool("serpapi", fake_redis, AsyncMock(), quota_default=100)
    await pool.reload()
    return pool
