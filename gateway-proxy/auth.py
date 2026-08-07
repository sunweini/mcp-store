"""Token authentication + permission checks.

Tokens are stored SHA-256 hashed in Redis (never plaintext). We hash the
incoming Bearer token and look up tokens:{hash}. Permissions are a JSON
map of {server: {read, write}}.

高并发下每请求 Redis hgetall 是瓶颈；缓存命中免 Redis（TTL 60s，LRU 上限
1000）。Redis 瞬时故障时缓存继续放行——否则 Redis 抖 = 全站 403 风暴（R8）。
失效：admin 变更 token 后 publish token:changed → proxy watch_changes 调
invalidate_token_cache（TTL 只兜底，撤销必须即时）。
"""
import hashlib
import json
import time
from collections import OrderedDict

from redis_client import get_redis

_CACHE_TTL = 60
_CACHE_MAX = 1000
_cache: "OrderedDict[str, tuple[float, dict | None]]" = OrderedDict()


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a token string."""
    return hashlib.sha256(token.encode()).hexdigest()


def _cache_get(token_hash: str) -> dict | None:
    entry = _cache.get(token_hash)
    if entry is None:
        return None
    ts, info = entry
    if time.time() - ts > _CACHE_TTL:
        del _cache[token_hash]
        return None
    _cache.move_to_end(token_hash)  # LRU
    return info


def _cache_put(token_hash: str, info: dict | None) -> None:
    _cache[token_hash] = (time.time(), info)
    _cache.move_to_end(token_hash)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def invalidate_token_cache(token_hash: str) -> None:
    """token:changed 通道回调：吊销/变更即时失效缓存。"""
    _cache.pop(token_hash, None)


def clear_token_cache() -> None:
    """测试用：清空缓存。"""
    _cache.clear()


async def verify_token(token: str) -> dict | None:
    """Look up a token by its hash. Returns token info dict or None if invalid.

    缓存命中免 Redis；未命中走 Redis 并回填缓存。语义与改造前一致
    （invalid token 也缓存为 None，防 Redis 空查风暴——注意：token 新建
    后最长 60s 生效，可接受）。

    Returns: {"id", "name", "permissions": {server: {read, write}}}
    """
    token_hash = hash_token(token)
    cached = _cache_get(token_hash)
    if cached is not None or token_hash in _cache:
        return cached
    r = get_redis()
    data = await r.hgetall(f"tokens:{token_hash}")
    if not data:
        _cache_put(token_hash, None)
        return None
    info = {
        "id": data["id"],
        "name": data["name"],
        "permissions": json.loads(data["permissions"]),
    }
    _cache_put(token_hash, info)
    return info


def check_permission(token_info: dict, server: str, mode: str) -> bool:
    """Check whether a token grants (server, mode) access.

    mode is 'read' or 'write'. No entry for server -> denied.
    """
    perm = token_info["permissions"].get(server)
    if not perm:
        return False
    return bool(perm.get(mode, False))
