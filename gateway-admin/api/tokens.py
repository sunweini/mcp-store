"""Token management API.

MCP client auth tokens. Stored SHA-256 hashed (never plaintext). The
plaintext is returned ONLY in the POST create response; list/detail
return a mask. gateway-proxy verifies by hashing the incoming token
and looking up tokens:{sha256}.
"""
import json
import time
import hashlib
import secrets
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin, mask_token
from redis_client import get_redis

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


class PermissionSpec(BaseModel):
    read: bool = False
    write: bool = False


class TokenCreate(BaseModel):
    name: str
    permissions: dict[str, PermissionSpec]


def generate_token() -> str:
    """Generate a random token string: tok_ + 24 url-safe chars."""
    return "tok_" + secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    """SHA-256 hex digest (matches gateway-proxy auth.hash_token)."""
    return hashlib.sha256(token.encode()).hexdigest()


@router.post("", status_code=201)
async def create_token(req: TokenCreate, _: str = Depends(require_admin)):
    r = get_redis()
    # Validate all referenced servers exist
    for server_name in req.permissions:
        if not await r.exists(f"servers:{server_name}"):
            raise HTTPException(status_code=422, detail=f"server '{server_name}' not registered")
    plaintext = generate_token()
    token_hash = hash_token(plaintext)
    token_id = "tokid_" + secrets.token_urlsafe(8)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    perms_serialized = {
        srv: {"read": p.read, "write": p.write} for srv, p in req.permissions.items()
    }
    await r.hset(f"tokens:{token_hash}", mapping={
        "id": token_id,
        "name": req.name,
        "token_hash": token_hash,
        "permissions": json.dumps(perms_serialized),
        "created_at": now,
    })
    await r.set(f"token_id:{token_id}", token_hash)
    # plaintext returned ONCE here; never again retrievable
    return {
        "id": token_id,
        "name": req.name,
        "token": plaintext,
        "warning": "明文只显示一次，请立即保存",
        "permissions": perms_serialized,
        "created_at": now,
    }


@router.get("")
async def list_tokens(_: str = Depends(require_admin)):
    r = get_redis()
    # Scan all token keys
    out = []
    async for key in r.scan_iter(match="tokens:*"):
        data = await r.hgetall(key)
        if data:
            out.append({
                "id": data.get("id", ""),
                "name": data.get("name", ""),
                "token_masked": mask_token(data.get("token_hash", "")),
                "permissions": json.loads(data.get("permissions", "{}")),
                "created_at": data.get("created_at", ""),
            })
    return out


@router.delete("/{token_id}", status_code=204)
async def delete_token(token_id: str, _: str = Depends(require_admin)):
    r = get_redis()
    token_hash = await r.get(f"token_id:{token_id}")
    if not token_hash:
        raise HTTPException(status_code=404, detail="token not found")
    await r.delete(f"tokens:{token_hash}")
    await r.delete(f"token_id:{token_id}")
    return None
