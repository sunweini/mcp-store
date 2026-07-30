"""Token authentication + permission checks.

Tokens are stored SHA-256 hashed in Redis (never plaintext). We hash the
incoming Bearer token and look up tokens:{hash}. Permissions are a JSON
map of {server: {read, write}}.
"""
import hashlib
import json
from redis_client import get_redis


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a token string."""
    return hashlib.sha256(token.encode()).hexdigest()


async def verify_token(token: str) -> dict | None:
    """Look up a token by its hash. Returns token info dict or None if invalid.

    Returns: {"id", "name", "permissions": {server: {read, write}}}
    """
    r = get_redis()
    data = await r.hgetall(f"tokens:{hash_token(token)}")
    if not data:
        return None
    return {
        "id": data["id"],
        "name": data["name"],
        "permissions": json.loads(data["permissions"]),
    }


def check_permission(token_info: dict, server: str, mode: str) -> bool:
    """Check whether a token grants (server, mode) access.

    mode is 'read' or 'write'. No entry for server -> denied.
    """
    perm = token_info["permissions"].get(server)
    if not perm:
        return False
    return bool(perm.get(mode, False))
