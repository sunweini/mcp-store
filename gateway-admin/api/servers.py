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
from mcp_probe import probe, introspect_tools

router = APIRouter(prefix="/api/servers", tags=["servers"])

# NOTE: underscores break namespace-prefix routing (split on first _).
_NAME_RE = re.compile(r"^[a-z0-9-]+$")


class ServerCreate(BaseModel):
    name: str
    url: str
    description: str = ""
    # 总超时秒（proxy 每请求 wait_for 上限）；None → proxy 默认 90s
    call_timeout: float | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("name must be [a-z0-9-] only, no underscores")
        return v


class ServerUpdate(BaseModel):
    url: str
    description: str = ""
    call_timeout: float | None = None


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
        # call_timeout None → 不写字段，proxy 挂载时走默认 90s
        **({"call_timeout": str(req.call_timeout)} if req.call_timeout is not None else {}),
    })
    await r.sadd("servers:active", req.name)
    await _publish_change("add", req.name)
    return {"name": req.name, "url": req.url, "description": req.description,
            "status": "active", "call_timeout": req.call_timeout}


@router.get("")
async def list_servers(_: str = Depends(require_admin)):
    r = get_redis()
    names = await r.smembers("servers:active")
    out = []
    for name in names:
        data = await r.hgetall(f"servers:{name}")
        if data:
            raw_ct = data.get("call_timeout")
            try:
                call_timeout = float(raw_ct) if raw_ct else None
            except (TypeError, ValueError):
                call_timeout = None
            out.append({
                "name": data.get("name", name),
                "url": data.get("url", ""),
                "description": data.get("description", ""),
                "status": data.get("status", "active"),
                "call_timeout": call_timeout,
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
    mapping = {"url": req.url, "description": req.description}
    if req.call_timeout is not None:
        mapping["call_timeout"] = str(req.call_timeout)
    else:
        # call_timeout=None 语义是「清除自定义超时，恢复 proxy 默认 90s」——
        # 只写不删会让 hash 残留旧值，proxy 永久沿用已改的超时（审查 Finding 1）
        await r.hdel(f"servers:{name}", "call_timeout")
    await r.hset(f"servers:{name}", mapping=mapping)
    await _publish_change("update", name)
    return {"name": name, "url": req.url, "description": req.description,
            "call_timeout": req.call_timeout}


@router.delete("/{name}", status_code=204)
async def delete_server(name: str, _: str = Depends(require_admin)):
    r = get_redis()
    if not await r.exists(f"servers:{name}"):
        raise HTTPException(status_code=404, detail="server not found")
    await r.delete(f"servers:{name}")
    await r.srem("servers:active", name)
    await _publish_change("remove", name)
    return None


@router.get("/{name}/status")
async def server_status(name: str, _: str = Depends(require_admin)):
    """Immediately probe a server and update its health in Redis."""
    r = get_redis()
    data = await r.hgetall(f"servers:{name}")
    if not data:
        raise HTTPException(status_code=404, detail="server not found")
    result = await probe(data["url"])
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await r.hset(f"servers:{name}", mapping={
        "health_up": "1" if result.up else "0",
        "health_latency_ms": str(result.latency_ms) if result.latency_ms is not None else "",
        "last_health_check": now,
    })
    return {"up": result.up, "latency_ms": result.latency_ms, "checked": now}


@router.post("/{name}/refresh-tools")
async def refresh_tools(name: str, _: str = Depends(require_admin)):
    """Re-introspect tools/list and store them in Redis."""
    r = get_redis()
    data = await r.hgetall(f"servers:{name}")
    if not data:
        raise HTTPException(status_code=404, detail="server not found")
    tools = await introspect_tools(data["url"])
    await r.hset(f"servers:{name}", "tools", json.dumps(tools))
    return {"name": name, "tools": tools}


_LIFECYCLE = {"disable": "disabled", "stop": "stopped", "enable": "active"}


class LifecycleAction(BaseModel):
    action: str


@router.post("/{name}/lifecycle")
async def set_lifecycle(name: str, req: LifecycleAction, _: str = Depends(require_admin)):
    """禁用/停用/启用 server：只改 status + 通知 gateway 热更新。

    停/起容器为人工操作（admin 不控 docker），此端点只管 gateway 清单。
    """
    r = get_redis()
    if not await r.exists(f"servers:{name}"):
        raise HTTPException(status_code=404, detail="server not found")
    if req.action not in _LIFECYCLE:
        raise HTTPException(status_code=422, detail=f"action must be one of {list(_LIFECYCLE)}")
    status = _LIFECYCLE[req.action]
    await r.hset(f"servers:{name}", "status", status)
    await _publish_change(req.action, name)
    return {"name": name, "status": status}
