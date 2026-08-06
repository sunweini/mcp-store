"""Shared fixtures: fakeredis（与 gateway-admin 测试同模式）。"""
import pytest
import fakeredis.aioredis


@pytest.fixture
async def fake_redis():
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield fake
    await fake.aclose()
