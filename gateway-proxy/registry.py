"""Server registry: mount/unmount backends + hot-reload via Redis Pub/Sub.

On startup we load all servers in servers:active and mount them. We then
subscribe to the 'server:changed' channel so the admin service can add/
update/remove servers without restarting the proxy.
"""
import json
import time
import structlog
import httpx
from dataclasses import dataclass

from redis_client import get_redis
from routing import register_tools, clear_tools

logger = structlog.get_logger()


@dataclass
class HealthResult:
    up: bool
    latency_ms: float | None


async def probe(url: str) -> HealthResult:
    """Ping a backend MCP server (standard MCP ping). 5s timeout."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
            return HealthResult(up=resp.status_code == 200, latency_ms=(time.monotonic() - start) * 1000)
    except httpx.HTTPError:
        return HealthResult(up=False, latency_ms=None)


async def _introspect_tools(url: str) -> list[dict]:
    """Call tools/list on a backend to learn each tool's mode + description.

    mode is 'write' if annotations.destructiveHint else 'read'.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            data = resp.json()
            tools = []
            for t in data.get("result", {}).get("tools", []):
                ann = t.get("annotations") or {}
                tools.append({
                    "name": t["name"],
                    "mode": "write" if ann.get("destructiveHint") else "read",
                    "description": t.get("description", ""),
                })
            return tools
    except httpx.HTTPError as e:
        logger.error("introspect_failed", url=url, error=str(e), service="gateway-proxy")
        return []


def parse_change_event(raw: str) -> tuple[str, str] | None:
    """Parse a server:changed pubsub message. Returns (action, name) or None."""
    try:
        evt = json.loads(raw)
        return evt["action"], evt["name"]
    except (json.JSONDecodeError, KeyError):
        return None


async def mount_all(gateway) -> None:
    """Load every server in servers:active and mount it (startup)."""
    r = get_redis()
    names = await r.smembers("servers:active")
    for name in names:
        info = await r.hgetall(f"servers:{name}")
        if info:
            await _mount_one(gateway, name, info["url"])


async def _mount_one(gateway, name: str, url: str) -> None:
    """Mount a single backend + introspect its tools into TOOL_REGISTRY."""
    from fastmcp import create_proxy
    try:
        gateway.mount(create_proxy(url), name=name)
    except Exception as e:
        logger.error("mount_failed", server=name, error=str(e), service="gateway-proxy")
        return
    tools = await _introspect_tools(url)
    register_tools(name, tools)
    # store tools back to redis for the admin UI to read
    r = get_redis()
    await r.hset(f"servers:{name}", "tools", json.dumps(tools))
    logger.info("server_mounted", server=name, tools=len(tools), service="gateway-proxy")


async def _unmount_one(gateway, name: str) -> None:
    """Remove a backend. FastMCP has no public unmount API in 4.0.0b1, so we
    filter the gateway's providers list to drop the namespaced one.

    A mounted server with namespace=N appears in gateway.providers as
    _WrappedProvider(..., transforms=[Namespace(N)]); Namespace exposes _prefix.
    """
    # Filter out the provider whose Namespace transform matches `name`.
    # LocalProvider (no transforms) and other namespaces are preserved.
    kept = []
    removed = False
    for p in gateway.providers:
        if _provider_namespace(p) == name:
            removed = True
            continue
        kept.append(p)
    if removed:
        gateway.providers = kept
    clear_tools(name)
    logger.info("server_unmounted", server=name, found=removed, service="gateway-proxy")


def _provider_namespace(provider) -> str | None:
    """Return the namespace prefix of a mounted provider, or None.

    mount(namespace=N) wraps the FastMCPProvider in a _WrappedProvider whose
    transforms list contains a Namespace instance with _prefix = N. LocalProvider
    and unwrapped providers have no namespace.
    """
    transforms = getattr(provider, "transforms", None) or []
    for t in transforms:
        prefix = getattr(t, "_prefix", None)
        if prefix:
            return prefix
    return None


async def watch_changes(gateway) -> None:
    """Subscribe to server:changed and hot-reload mounts. Runs forever."""
    r = get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe("server:changed")
    async for msg in pubsub.listen():
        if msg.get("type") != "message":
            continue
        parsed = parse_change_event(msg["data"])
        if not parsed:
            continue
        action, name = parsed
        info = await r.hgetall(f"servers:{name}")
        if action in ("add", "update") and info:
            await _unmount_one(gateway, name)
            await _mount_one(gateway, name, info["url"])
        elif action == "remove":
            await _unmount_one(gateway, name)
