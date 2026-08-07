"""Async Redis connection singleton.

Shared by auth, registry, audit. Module-level so all modules see one pool.
"""
import os
import redis.asyncio as redis

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Return the process-level Redis client. Lazily initialized."""
    global _redis
    if _redis is None:
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        # socket_timeout: Redis 挂起（网络分区/主从切换）不能阻塞请求路径
        # （verify_token/审计 XADD 都在关键路径上）。5s 足以覆盖正常 ops，
        # 挂起时快速失败而非无限等。
        _redis = redis.from_url(url, decode_responses=True, socket_timeout=5)
    return _redis


async def close_redis() -> None:
    """Close the Redis connection pool on shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
