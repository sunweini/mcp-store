"""Shared test fixtures.

Uses a process-local fake instead of real Redis so unit tests need no broker.
"""
import pytest
import fakeredis.aioredis


@pytest.fixture
async def fake_redis(monkeypatch):
    """Replace get_redis() with an in-memory fake Redis."""
    import redis_client
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "_redis", fake)
    yield fake
    await fake.aclose()
