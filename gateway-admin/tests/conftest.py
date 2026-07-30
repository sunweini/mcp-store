"""Shared fixtures: fake Redis + FastAPI TestClient override."""
import pytest
import fakeredis.aioredis
from fastapi.testclient import TestClient


@pytest.fixture
async def fake_redis(monkeypatch):
    import redis_client
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "_redis", fake)
    yield fake
    await fake.aclose()


@pytest.fixture
def client(fake_redis):
    """Synchronous FastAPI TestClient (uses anyio portal for async lifespan).

    Depends on fake_redis so monkeypatch is applied before lifespan runs
    ensure_default_admin() which calls get_redis().
    """
    from app import app
    with TestClient(app) as c:
        yield c
