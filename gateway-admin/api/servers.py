"""Server management API.

Writes server config to Redis + notifies gateway-proxy via Pub/Sub
(server:changed channel) so it hot-reloads the mount.
"""
import json
import re
import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from auth import require_admin
from redis_client import get_redis

router = APIRouter(prefix="/api/servers", tags=["servers"])

# NOTE: underscores break namespace-prefix routing (split on first _).
_NAME_RE = re.compile(r"^[a-z0-9-]+$")


class ServerCreate(BaseModel):
    name: str
    url: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("name must be [a-z0-9-] only, no underscores")
        return v


class ServerUpdate(BaseModel):
    url: str
    description: str = ""


async def _publish_change(action: str, name: str) -> None:
    """Notify gateway-proxy to hot-reload this server."""
    r = get_redis()
    await r.publish("server:changed", json.dumps({"action": action, "name": name}))


@router.post("", status_code=201)
async def create_server(req: ServerCreate, _: str = Depends(require_admin)):
    r = get_redis()
    if await r.exists(f"servers:{req.name}"):
        raise HTTPException(status_code=409, detail=f"server '{req.name}' already exists")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await r.hset(f"servers:{req.name}", mapping={
        "name": req.name, "url": req.url, "description": req.description,
        "status": "active", "tools": "[]",
        "health_up": "0", "health_latency_ms": "", "last_health_check": "",
        "created_at": now,
    })
    await r.sadd("servers:active", req.name)
    await _publish_change("add", req.name)
    return {"name": req.name, "url": req.url, "description": req.description, "status": "active"}


@router.get("")
async def list_servers(_: str = Depends(require_admin)):
    r = get_redis()
    names = await r.smembers("servers:active")
    out = []
    for name in names:
        data = await r.hgetall(f"servers:{name}")
        if data:
            out.append({
                "name": data.get("name", name),
                "url": data.get("url", ""),
                "description": data.get("description", ""),
                "status": data.get("status", "active"),
                "health": {
                    "up": data.get("health_up") == "1",
                    "latency_ms": float(data["health_latency_ms"]) if data.get("health_latency_ms") else None,
                    "last_check": data.get("last_health_check", ""),
                },
                "tools": json.loads(data.get("tools", "[]")),
                "created_at": data.get("created_at", ""),
            })
    return out


@router.put("/{name}")
async def update_server(name: str, req: ServerUpdate, _: str = Depends(require_admin)):
    r = get_redis()
    if not await r.exists(f"servers:{name}"):
        raise HTTPException(status_code=404, detail="server not found")
    await r.hset(f"servers:{name}", mapping={"url": req.url, "description": req.description})
    await _publish_change("update", name)
    return {"name": name, "url": req.url, "description": req.description}


@router.delete("/{name}", status_code=204)
async def delete_server(name: str, _: str = Depends(require_admin)):
    r = get_redis()
    if not await r.exists(f"servers:{name}"):
        raise HTTPException(status_code=404, detail="server not found")
    await r.delete(f"servers:{name}")
    await r.srem("servers:active", name)
    await _publish_change("remove", name)
    return None
