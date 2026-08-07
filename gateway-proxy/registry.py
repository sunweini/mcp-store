"""Server registry: mount/unmount backends + hot-reload via Redis Pub/Sub.

On startup we load all servers in servers:active and mount them. We then
subscribe to the 'server:changed' channel so the admin service can add/
update/remove servers without restarting the proxy.
"""
import asyncio
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

# ── Task 4: Client 复用 + per-server 超时缓存 ────────────────────
# url → 共享 Client（transport 连接池复用）。FastMCP 默认 create_proxy(url)
# 每次请求新建 Client + httpx2 连接池（_httpx_utils.py，已核实）——每请求
# 一次 TCP+TLS 握手。缓存 Client 实例 + 共享 httpx_client_factory 后，
# 连接池跨请求复用（keepalive 连接不关）。
# name → url：unmount 时定位缓存 Client 显式关闭（原实现靠 GC，连接泄漏）。
_mounted_clients: dict[str, object] = {}
_mounted_urls: dict[str, str] = {}
# name → call_timeout 秒（挂载/更新时从 servers:{name} hash 缓存，
# 每请求读 Redis 会放大请求路径延迟，必须缓存）。
_mounted_timeouts: dict[str, float] = {}

# 默认总超时 90s：后端最长长任务 tavily research 60s + 余量。
# 绝不能 30s（会把 tavily 长任务全部掐死）。
_CALL_TIMEOUT_DEFAULT = 90.0
# pubsub 断连后的重连退避（秒）。模块级可配置——测试用短退避加速。
_PUBSUB_RETRY_DELAY = 5.0


def _shared_httpx_factory():
    """返回共享 httpx2.AsyncClient 工厂，供所有 proxy 后端复用连接池。

    为什么必须传 httpx_client_factory：StreamableHttpTransport 每次
    connect_session 新建 AsyncClient，session 退出时 async with 的
    __aexit__ 关闭其连接池（httpcore2 连接池是 per-AsyncClient 实例的，
    无跨实例共享）——每请求一次 TCP+TLS 握手，即本任务 P0。只有显式传入
    httpx_client_factory 的分支才跳过默认新建路径（http.py 三分支中
    factory 分支优先）。

    共享实现：每次调用返回新 AsyncClient，但所有实例共享同一个
    _ReusableTransport 包裹的 httpcore2.AsyncConnectionPool。连接池按
    (host, port) 键控：各后端连接互不干扰。放大 max_connections 到 100
    （httpcore2 默认 10 会限制单 server 并发）。

    _ReusableTransport 为什么必须存在（实测）：
    1. httpcore2 pool 直接当 httpx2 transport 传会在请求时炸——
       httpx2 Request 的 url.scheme 是 str，httpcore2 要 bytes
       （AttributeError: 'str' object has no attribute 'decode'）。
       AsyncHTTPTransport.handle_async_request 负责两种 Request 互转。
    2. 默认 __aexit__/aclose 会 aclose() 掉池里全部连接（http.py 的
       `async with http_client` 每次都触发）——连接复用落空。这里吞掉，
       连接生命周期归共享池，进程存活期间常驻。
    """
    import httpx2
    import httpcore2

    class _ReusableTransport(httpx2.AsyncHTTPTransport):
        """共享连接池 transport：session 退出（AsyncClient.__aexit__）不关池。

        不调用 super().__init__（不需要内部自带池），只挂共享 pool；
        请求转换逻辑（httpx2 Request ↔ httpcore2 Request）继承默认实现。
        实验验证（本机 keep-alive 服务器）：三次独立 session 后
        pool._connections 保持 1，连接跨 session 复用。
        """
        def __init__(self, pool):
            self._pool = pool
        async def __aexit__(self, *exc_info):
            # 共享池不随单个 session 关闭——否则每请求一次 TCP+TLS
            return None
        async def aclose(self):
            pass

    transport = _ReusableTransport(
        httpcore2.AsyncConnectionPool(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=30.0,
        )
    )

    def factory(*, headers=None, auth=None, follow_redirects=None, **kwargs):
        return httpx2.AsyncClient(
            transport=transport,
            headers=headers,
            auth=auth,
            follow_redirects=True if follow_redirects is None else follow_redirects,
            timeout=httpx2.Timeout(30.0, read=300.0),
            **kwargs,
        )

    return factory


def _get_or_create_client(url: str):
    """返回缓存 ProxyClient 或新建。

    缓存的是 base client：每请求由 client_factory 的 new() 派生
    （fresh session），base 自身保持 disconnected 且 mode 不变——mirror
    语义（_mirror_front_era_mode）在派生实例上应用，不受缓存影响。

    httpx_client_factory 为什么挂在 transport 上：Client 的 kwargs 没有
    这个参数（实测 TypeError），它在 StreamableHttpTransport 上——官方
    create_proxy 的 URL 分支由 infer_transport 无 factory 构造，我们
    显式构造 transport 传入共享 factory（连接池复用，即 P0）。new()
    是 copy 派生，共享 transport 引用，factory 跨请求生效。
    共享 Client 无默认 Authorization 头（key 串用防护 R5，后端鉴权走
    自己的认证）。
    """
    from fastmcp.client.transports.http import StreamableHttpTransport
    from fastmcp.server.providers.proxy import ProxyClient

    client = _mounted_clients.get(url)
    if client is None:
        transport = StreamableHttpTransport(
            url=url,
            httpx_client_factory=_shared_httpx_factory(),
        )
        client = ProxyClient(transport)
        _mounted_clients[url] = client
    return client


def _make_client_factory(url: str):
    """返回 client_factory：保留 create_proxy 的 era mirror 语义 + 连接池复用。

    create_proxy 默认 factory（proxy_client_factory）每请求：
    1. base_client.new() 派生 fresh client（新 session，transport 共享）
    2. mirror 前端协商的 era 到 fresh.mode（_mirror_front_era_mode）

    我们把 base_client 换成缓存实例（_get_or_create_client）：
    - new() 是 copy 派生，每次请求仍是 fresh session（无 R11 上下文串扰）
    - 派生实例共享缓存 base 的 transport + httpx_client_factory（连接池）
    - mirror 语义在派生实例上应用，行为与默认完全一致
    - 挂载期只建一次 factory，零每请求开销

    为什么不用 create_proxy(Client 实例) 或 create_proxy(client_factory=)：
    - create_proxy 的 target 是必填位置参数，client_factory 不是它的参数
      （实测 TypeError：create_proxy() missing required positional argument）
    - 传 disconnected Client 走 as_proxy_backend（replace+copy 派生），
      每请求一次 copy 开销且不自带连接池复用
    - FastMCPProxy(client_factory=) 是官方扩展点（"full control over
      session creation and reuse"），create_proxy 只是它的薄封装，
      等价且语义最贴合
    """
    from fastmcp.server.providers.proxy import (
        _mirror_front_era_mode,
        PROXY_TRANSPORT_OPTIONS,
    )
    from dataclasses import replace

    def factory():
        # 缓存 base client：transport/连接池跨请求复用
        fresh = _get_or_create_client(url).new()
        # mirror 前端协商 era（与官方 proxy_client_factory 完全一致）
        backend_mode = _mirror_front_era_mode()
        if backend_mode is not None:
            fresh.mode = backend_mode
        # 多 server MCPConfig 目标需要把 era 带到后端各腿
        fresh._transport_options = replace(
            PROXY_TRANSPORT_OPTIONS, backend_mode=fresh.mode
        )
        return fresh

    return factory


def _get_call_timeout(name: str) -> float:
    """返回 server 的总超时秒数（挂载时缓存的 call_timeout，缺省 90s）。"""
    return _mounted_timeouts.get(name, _CALL_TIMEOUT_DEFAULT)


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
    # C1: FastMCPProxy lives in fastmcp.server.providers.proxy; create_proxy
    # (fastmcp.server) 只是它的薄封装——但 create_proxy 的 target 是必填
    # 位置参数，不接受 client_factory（实测 TypeError），故直接构造
    # FastMCPProxy 以传入自定义 client_factory。
    from fastmcp.server.providers.proxy import FastMCPProxy
    try:
        # C2: mount uses namespace= (not name=). name= is silently ignored and
        # the proxy is mounted under the empty namespace, breaking routing.
        #
        # Task 4: client_factory 复用——每次工具调用经 _make_client_factory
        # 从缓存 base client new() 派生（fresh session + transport 连接池
        # 复用），不再每请求新建 Client + httpx2 连接池（每请求一次
        # TCP+TLS 握手，即本任务 P0）。
        # 预创建缓存 client：挂载即建（连接池随挂载建立），而非等首个
        # 请求才懒创建——unmount 时能显式关闭，disable→enable 热切换时
        # 复用同池（不重建连接）。
        _get_or_create_client(url)
        proxy = FastMCPProxy(client_factory=_make_client_factory(url))
        gateway.mount(proxy, namespace=name)
        _mounted_urls[name] = url
    except Exception as e:
        logger.error("mount_failed", server=name, error=str(e), service="gateway-proxy")
        return
    tools = await _introspect_tools(url)
    register_tools(name, tools)
    # store tools back to redis for the admin UI to read
    r = get_redis()
    await r.hset(f"servers:{name}", "tools", json.dumps(tools))
    # 缓存 per-server 总超时（挂载时读一次 Redis，请求路径不再读）
    try:
        raw = await r.hget(f"servers:{name}", "call_timeout")
        if raw:
            _mounted_timeouts[name] = float(raw)
        else:
            _mounted_timeouts.pop(name, None)
    except (TypeError, ValueError) as e:
        logger.warning("call_timeout_invalid", server=name, error=str(e), service="gateway-proxy")
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
    # Task 4: 显式关闭缓存 Client 的连接池（原实现靠 GC，连接泄漏）。
    # 同 url 重挂载（disable→enable）会复用缓存 Client——连接池保持热。
    url = _mounted_urls.pop(name, None)
    _mounted_timeouts.pop(name, None)
    client = _mounted_clients.pop(url, None) if url else None
    if client is not None:
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as e:
                logger.warning(
                    "client_close_failed", server=name, error=str(e), service="gateway-proxy"
                )
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
    """订阅 server:changed + token:changed，热加载 server + 失效 token 缓存。

    双频道复用同一条 pubsub 连接（redis-py 支持多频道 subscribe），
    自愈逻辑只维护一个连接——避免第二条连接带来双倍断线面。

    Task 4 自愈：redis-py 的 pubsub listen() 断连时抛异常退出循环且不会
    自动重连（tavily 正式环境踩过：Redis 重启后热更新永久失效，只能重启
    容器）。这里 while True 包一层：listen 退出（断连/异常）→ aclose 旧
    pubsub 防连接泄漏 → 重建 + 重新订阅双频道。重建期间 Redis 不可用则
    吞掉异常并退避重试——循环必须永远存活，这是热加载的命脉。
    """
    r = get_redis()
    while True:
        pubsub = r.pubsub()
        try:
            await pubsub.subscribe("server:changed", "token:changed")
        except Exception:
            # Redis 未恢复时 pubsub()/subscribe 同样抛错——吞掉让外层
            # 退避重试；此处若抛出会杀死 watcher 任务，回到只能重启
            # 容器的老问题
            try:
                await pubsub.aclose()
            except Exception:
                pass
            logger.warning("pubsub_subscribe_failed", service="gateway-proxy")
            await asyncio.sleep(_PUBSUB_RETRY_DELAY)
            continue
        logger.info("pubsub_subscribed", channels=["server:changed", "token:changed"], service="gateway-proxy")
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                # M1: isolate each event so one bad message can't kill hot-reload.
                try:
                    channel = msg.get("channel", "")
                    if channel == "token:changed":
                        from auth import invalidate_token_cache
                        data = json.loads(msg["data"])
                        invalidate_token_cache(data["token_hash"])
                        continue
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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # listen 断连：pubsub 底层连接已死，close 防连接泄漏后重建
            logger.warning(
                "pubsub_disconnected",
                error=str(e),
                error_type=type(e).__name__,
                service="gateway-proxy",
            )
        finally:
            try:
                await pubsub.aclose()
            except Exception:
                pass
        # 断连后退避重连（listen 正常 EOF 结束也走这里，无需区分）
        await asyncio.sleep(_PUBSUB_RETRY_DELAY)
