"""Shared httpx mock transport + pool fixture for brave tests."""
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


class MockTransportFactory:
    """Callable factory that also exposes the last created transport's
    request. Tests do `mock_transport(payload)` to build a transport and
    `mock_transport.last_request` to inspect what was sent — the brief's
    tests rely on the fixture itself being callable AND observable.
    """

    def __init__(self):
        self._transport = None

    def __call__(self, payload=None, status_code=200, headers=None):
        self._transport = MockTransport(payload or {}, status_code, headers)
        return self._transport

    @property
    def last_request(self):
        return self._transport.last_request if self._transport else None


@pytest.fixture
def mock_transport():
    return MockTransportFactory()


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

    def pipeline(self):
        # Task 6：on_success 的 hset+zadd+expire 三连走 pipeline 一次往返。
        # 委托到 FakeRedis 单命令（复用 hset_calls/zadd_calls/expire_calls），
        # execute 计数供「合并为一次往返」断言（同 test_key_pool 形态）
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


@pytest.fixture
async def fake_pool():
    """App-level KeyPool fixture: real KeyPool over FakeRedis, two keys.

    k1 剩余 900 > k2 剩余 800 —— next_key 默认返回 k1（与 test_key_pool
    的挑选逻辑一致）。工具层测试以此验证 pool 集成（成功/失败记账、
    key 失效后 failover 到 k2）。
    """
    records = {
        "k1": json.dumps({"key": "BSA-a", "provider": "brave", "enabled": True,
                          "monthly_quota": 2000, "status": "active",
                          "cooldown_until": None, "remaining": 1900, "last_error": None}),
        "k2": json.dumps({"key": "BSA-b", "provider": "brave", "enabled": True,
                          "monthly_quota": 2000, "status": "active",
                          "cooldown_until": None, "remaining": 1800, "last_error": None}),
    }
    fake_redis = FakeRedis(records)
    from unittest.mock import AsyncMock
    pool = KeyPool("brave", fake_redis, AsyncMock(), quota_default=2000)
    await pool.reload()
    return pool
