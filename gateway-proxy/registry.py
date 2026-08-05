"""Server registry: mount/unmount backends + hot-reload via Redis Pub/Sub.

On startup we load all servers in servers:active and mount them. We then
subscribe to the 'server:changed' channel so the admin service can add/
update/remove servers without restarting the proxy.
"""
import json
import re
import time
import structlog
import httpx
from dataclasses import dataclass

from redis_client import get_redis
from routing import register_tools, clear_tools

logger = structlog.get_logger()

# Server names are lower-case [a-z0-9-]+ so they are URL-safe and contain no
# underscores - routing.split_prefix splits on the first underscore, so an
# underscore in the namespace would break tool routing.
_SERVER_NAME_RE = re.compile(r"^[a-z0-9-]+$")


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

    NOTE: FastMCP servers return SSE format (event: message\ndata: {...}),
    not plain JSON. Parse the data: line to extract the JSON-RPC response.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            text = resp.text
            # FastMCP returns SSE: "event: message\ndata: {...}\n\n"
            # Extract JSON from the data: line
            data = None
            for line in text.split("\n"):
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    break
            if data is None:
                # Fallback: try plain JSON (non-FastMCP backends)
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
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as e:
        # Why catch JSONDecodeError + ValueError: a backend may return HTML /
        # plain text on error (e.g. 502 from a reverse proxy). Without this,
        # a non-JSON body would crash watch_changes and kill hot-reload.
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
    """Load every server in servers:active and mount it (startup).

    Mounts are serial on purpose: parallel mount would interleave introspection
    logs and make startup order non-deterministic, which is hard to debug when
    a single backend is misconfigured. Startup is not on the hot path.
    """
    r = get_redis()
    names = await r.smembers("servers:active")
    for name in names:
        info = await r.hgetall(f"servers:{name}")
        if not info:
            logger.warning("mount_all_skip", server=name, reason="no info hash", service="gateway-proxy")
            continue
        url = info.get("url")
        if not url:
            logger.warning("mount_all_skip", server=name, reason="missing url", service="gateway-proxy")
            continue
        # 仅 active 挂载；旧数据无 status 字段默认 active（向后兼容）。
        # disabled/stopped 的 server 留在 servers:active set 里但不挂载，
        # 重启后保持未挂载状态而不是被意外恢复。
        status = info.get("status", "active")
        if status != "active":
            logger.warning("mount_all_skip", server=name, reason=f"status={status}", service="gateway-proxy")
            continue
        await _mount_one(gateway, name, url)


async def _sync_one(gateway, name: str, info: dict) -> None:
    """按 status 同步挂载：先卸载，仅 active 且有 url 才挂载。

    先 unmount 再 mount 保证 disable->enable 切换后 provider 是新的。
    """
    await _unmount_one(gateway, name)
    url = info.get("url")
    if info.get("status", "active") == "active" and url:
        await _mount_one(gateway, name, url)


async def _mount_one(gateway, name: str, url: str) -> None:
    """Mount a single backend + introspect its tools into TOOL_REGISTRY."""
    # I5: reject names with underscores / uppercase so split_prefix keeps working.
    if not _SERVER_NAME_RE.match(name):
        logger.error(
            "mount_skip_invalid_name",
            server=name,
            reason="name must match ^[a-z0-9-]+$",
            service="gateway-proxy",
        )
        return
    # C1: create_proxy lives in fastmcp.server, not the top-level fastmcp module.
    from fastmcp.server import create_proxy
    try:
        # C2: mount uses namespace= (not name=). name= is silently ignored and
        # the proxy is mounted under the empty namespace, breaking routing.
        gateway.mount(create_proxy(url), namespace=name)
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

    TODO: close client pool when FastMCP exposes a public unmount(). The proxy
    client created by create_proxy() owns an httpx connection pool; dropping
    the reference here does not explicitly close it, so connections rely on GC.
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
        # M1: isolate each event so one bad message can't kill hot-reload.
        try:
            parsed = parse_change_event(msg["data"])
            if not parsed:
                continue
            action, name = parsed
            info = await r.hgetall(f"servers:{name}")
            if action == "remove":
                await _unmount_one(gateway, name)
            # enable/disable/stop 与 add/update 共用 _sync_one：统一按 hash 里
            # 的最新 status 决定挂载/卸载，避免每种 action 各写一份状态判断。
            elif action in ("add", "update", "enable", "disable", "stop") and info:
                await _sync_one(gateway, name, info)
        except Exception as e:
            logger.error("watch_changes_event_failed", error=str(e), service="gateway-proxy")
