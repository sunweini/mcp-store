"""Tests for server registry: probe, parse_change_event, mount/unmount."""
from registry import (
    probe,
    parse_change_event,
    _mount_one,
    _unmount_one,
    _introspect_tools,
    _provider_namespace,
    mount_all,
)
import asyncio
import json
import pytest
import routing


async def async_noop(*args, **kwargs):
    """No-op async for monkeypatching sync machinery out of watch_changes."""


async def pubsub_send(fake_redis, channel: str, payload: str) -> None:
    """向 channel publish。调用方须保证 watch_changes 的 subscribe 已完成
    （测试用 presubscribed_pubsub 夹具消除竞态），否则消息静默丢失。"""
    await fake_redis.publish(channel, payload)


async def wait_for(predicate, timeout: float = 5.0) -> None:
    """轮询等待 predicate() 为真（pubsub 消息异步送达，无法精确 await）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met within timeout")


async def presubscribed_pubsub(fake_redis, monkeypatch, registry):
    """让 watch_changes 复用测试侧预订阅的 pubsub，消除启动竞态。

    watch_changes 内部自己 `r.pubsub() + subscribe`，若消息在 subscribe
    完成前 publish 会静默丢失（create_task 后首个 await 点不确定）。
    这里预订阅好双频道，monkeypatch registry.get_redis 返回包装对象——
    watch_changes 拿到即已就绪的 pubsub，测试 publish 不再有竞态。
    """
    ps = fake_redis.pubsub()
    await ps.subscribe("server:changed", "token:changed")

    class WrappedRedis:
        """get_redis 的替代：pubsub 返回预订阅实例，其余委托 fake。"""
        def pubsub(self):
            return ps
        async def hgetall(self, key):
            return await fake_redis.hgetall(key)

    monkeypatch.setattr(registry, "get_redis", lambda: WrappedRedis())
    return ps


async def test_probe_up(monkeypatch):
    # probe hits a URL with MCP ping; mock httpx to return 200. probe does not
    # touch Redis, so no fake_redis fixture is needed.
    import httpx
    async def fake_post(self, url, json=None):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is True
    assert result.latency_ms >= 0


async def test_probe_down(monkeypatch):
    import httpx
    async def fake_post(self, url, json=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is False


def test_parse_change_event_add():
    evt = parse_change_event('{"action":"add","name":"zabbix"}')
    assert evt == ("add", "zabbix")


def test_parse_change_event_invalid():
    assert parse_change_event("not json") is None


# ---------------------------------------------------------------------------
# I1: integration test - actually mount + unmount against a real FastMCP gateway.
# This is the proof that C1 (create_proxy import path) and C2 (namespace= kwarg)
# are fixed. If either regresses, this test fails at mount time.
# ---------------------------------------------------------------------------
async def test_mount_unmount_integration(fake_redis):
    """End-to-end mount + unmount against a real FastMCP('test') gateway.

    Uses a dummy URL (nothing is listening on :9999) - create_proxy() does not
    make a network call at construction time, only when a tool is invoked, so
    mount succeeds. _introspect_tools hits the URL and fails gracefully (I2),
    returning [] so TOOL_REGISTRY gets an empty dict for the server.
    """
    from fastmcp import FastMCP

    gateway = FastMCP("test")
    # Sanity: before mount, only LocalProvider is present.
    assert not any(_provider_namespace(p) == "zabbix" for p in gateway.providers)
    routing.TOOL_REGISTRY.pop("zabbix", None)

    await _mount_one(gateway, "zabbix", "http://localhost:9999/mcp")

    # After mount: a provider with namespace='zabbix' must exist.
    zabbix_providers = [p for p in gateway.providers if _provider_namespace(p) == "zabbix"]
    assert len(zabbix_providers) == 1, f"expected 1 zabbix provider, got {len(zabbix_providers)}"
    # And TOOL_REGISTRY has an entry (empty - introspect failed, which is fine).
    assert "zabbix" in routing.TOOL_REGISTRY

    await _unmount_one(gateway, "zabbix")

    # After unmount: provider gone, TOOL_REGISTRY cleared.
    zabbix_providers = [p for p in gateway.providers if _provider_namespace(p) == "zabbix"]
    assert len(zabbix_providers) == 0, "zabbix provider still present after unmount"
    assert "zabbix" not in routing.TOOL_REGISTRY, "TOOL_REGISTRY not cleared after unmount"


# ---------------------------------------------------------------------------
# I5: names with underscores / uppercase must be rejected (split_prefix safety).
# ---------------------------------------------------------------------------
async def test_mount_rejects_invalid_name(fake_redis):
    from fastmcp import FastMCP
    gateway = FastMCP("test")
    # underscore is the split_prefix separator - must be rejected.
    await _mount_one(gateway, "my_server", "http://localhost:9999/mcp")
    assert not any(_provider_namespace(p) == "my_server" for p in gateway.providers)
    assert "my_server" not in routing.TOOL_REGISTRY

    # uppercase also rejected.
    await _mount_one(gateway, "Zabbix", "http://localhost:9999/mcp")
    assert not any(_provider_namespace(p) == "Zabbix" for p in gateway.providers)


# ---------------------------------------------------------------------------
# I2: a non-JSON backend response (e.g. HTML 502 from a reverse proxy) must
# not crash _introspect_tools - it should return [].
# ---------------------------------------------------------------------------
async def test_introspect_non_json(monkeypatch):
    import httpx

    async def fake_post(self, url, json=None):
        # Simulate a reverse proxy returning an HTML error page.
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    tools = await _introspect_tools("http://localhost:9999/mcp")
    assert tools == []


# ---------------------------------------------------------------------------
# I3: mount_all must skip servers with no info hash or missing url, not crash.
# ---------------------------------------------------------------------------
async def test_mount_all_skips_missing_url(fake_redis):
    from fastmcp import FastMCP
    gateway = FastMCP("test")

    # Add a server to servers:active but give it no 'url' field.
    await fake_redis.sadd("servers:active", "broken")
    await fake_redis.hset("servers:broken", mapping={"name": "broken"})

    await mount_all(gateway)
    # No provider should have been mounted for 'broken'.
    assert not any(_provider_namespace(p) == "broken" for p in gateway.providers)
    assert "broken" not in routing.TOOL_REGISTRY


# ---------------------------------------------------------------------------
# I3 + I2 combined: a server whose url is present but unreachable still
# mounts successfully (introspection fails gracefully).
# ---------------------------------------------------------------------------
async def test_mount_one_url_unreachable(fake_redis):
    from fastmcp import FastMCP
    gateway = FastMCP("test")
    routing.TOOL_REGISTRY.pop("zabbix", None)

    await _mount_one(gateway, "zabbix", "http://localhost:9999/mcp")
    # Provider mounted even though introspect failed.
    assert any(_provider_namespace(p) == "zabbix" for p in gateway.providers)
    # Empty tool list registered (not absent).
    assert routing.TOOL_REGISTRY.get("zabbix") == {}

    # Cleanup.
    await _unmount_one(gateway, "zabbix")
    assert "zabbix" not in routing.TOOL_REGISTRY


# ─── status 挂载控制 ─────────────────────────────────────────────

@pytest.fixture
def mount_log(monkeypatch):
    """记录 _mount_one/_unmount_one 调用，避免真连后端。"""
    import registry
    log = {"mount": [], "unmount": []}
    async def fake_mount(gw, name, url): log["mount"].append((name, url))
    async def fake_unmount(gw, name): log["unmount"].append(name)
    monkeypatch.setattr(registry, "_mount_one", fake_mount)
    monkeypatch.setattr(registry, "_unmount_one", fake_unmount)
    return log


class FakeGW: pass


async def test_mount_all_skips_non_active(fake_redis, mount_log):
    import registry
    await fake_redis.sadd("servers:active", "a", "b", "c")
    await fake_redis.hset("servers:a", mapping={"url": "http://a", "status": "active"})
    await fake_redis.hset("servers:b", mapping={"url": "http://b", "status": "disabled"})
    await fake_redis.hset("servers:c", mapping={"url": "http://c", "status": "stopped"})
    await registry.mount_all(FakeGW())
    assert mount_log["mount"] == [("a", "http://a")]


async def test_mount_all_default_active_when_no_status(fake_redis, mount_log):
    """旧数据无 status 字段 -> 默认 active（兼容）。"""
    import registry
    await fake_redis.sadd("servers:active", "old")
    await fake_redis.hset("servers:old", mapping={"url": "http://old"})
    await registry.mount_all(FakeGW())
    assert mount_log["mount"] == [("old", "http://old")]


# ─── Task 4: Client 复用 + unmount 显式关闭 ─────────────────────

async def test_mount_reuses_client(fake_redis, monkeypatch):
    """同 URL 多次 _get_or_create_client 只创建 1 个底层 Client（连接池复用）。

    _mount_one 通过 _make_client_factory → _get_or_create_client 拿缓存
    Client；同 URL 重复挂载（disable→enable 热切换）必须复用缓存实例，
    不能每次新建（每新建一个 ProxyClient 意味着新连接池，回到每请求
    TCP+TLS 的老问题）。
    """
    from fastmcp.server.providers.proxy import ProxyClient
    import registry

    created: list[ProxyClient] = []
    real_get = registry._get_or_create_client

    def counting_get(url: str):
        # 包一层真实实现：新建（缓存未命中）时计数
        client = real_get(url)
        if id(client) not in [id(c) for c in created]:
            created.append(client)
        return client

    monkeypatch.setattr(registry, "_get_or_create_client", counting_get)
    registry._mounted_clients.clear()
    registry._mounted_urls.clear()

    c1 = registry._get_or_create_client("http://backend:9050/mcp")
    c2 = registry._get_or_create_client("http://backend:9050/mcp")
    assert c1 is c2, "same URL must return the cached client instance"
    # 不同 URL 各自创建
    c3 = registry._get_or_create_client("http://backend:9051/mcp")
    assert c3 is not c1
    assert len(created) == 2, f"expected 2 unique clients, got {len(created)}"

    # 挂载路径端到端：同 URL 两次 _mount_one 共用同一缓存
    from fastmcp import FastMCP
    registry._mounted_clients.clear()
    registry._mounted_urls.clear()
    gateway = FastMCP("test")
    await registry._mount_one(gateway, "srv-a", "http://backend:9050/mcp")
    await registry._unmount_one(gateway, "srv-a")  # 模拟热切换的卸载
    await registry._mount_one(gateway, "srv-a", "http://backend:9050/mcp")
    # 热切换后缓存里仍是同一个 Client 实例（连接池保持热）
    assert len([k for k in registry._mounted_clients if k == "http://backend:9050/mcp"]) == 1


async def test_unmount_closes_client(fake_redis, monkeypatch):
    """_unmount_one 显式关闭缓存 Client（原实现靠 GC，连接泄漏）。"""
    from fastmcp import FastMCP
    import registry

    closed: list[str] = []

    class FakeClient:
        def __init__(self, url):
            self.url = url
        async def close(self):
            closed.append(self.url)
        def new(self):
            return self

    monkeypatch.setattr(registry, "_get_or_create_client",
                        lambda url: FakeClient(url))
    registry._mounted_clients.clear()
    registry._mounted_urls.clear()

    gateway = FastMCP("test")
    await registry._mount_one(gateway, "srv-a", "http://backend:9050/mcp")
    # 手动塞缓存（_mount_one 不触发 factory，缓存由 factory 懒填充）
    registry._mounted_clients["http://backend:9050/mcp"] = FakeClient("http://backend:9050/mcp")
    registry._mounted_urls["srv-a"] = "http://backend:9050/mcp"

    await registry._unmount_one(gateway, "srv-a")

    assert closed == ["http://backend:9050/mcp"], f"client not closed, got {closed}"
    assert "srv-a" not in registry._mounted_urls
    assert "http://backend:9050/mcp" not in registry._mounted_clients


# ─── pubsub 自愈 ────────────────────────────────────────────────

async def test_watch_changes_pubsub_resubscribe(fake_redis, monkeypatch):
    """listen 断连（抛异常退出）后重建订阅，双频道继续收消息。

    redis-py 的 pubsub listen() 断连时抛异常退出循环，不会自动重连。
    watch_changes 必须捕获后重建 pubsub + 重新订阅双频道。本测试：
    pubsub #1 的 listen 抛 ConnectionError（模拟断连）→ watch_changes
    创建 pubsub #2 → 后续 publish 的消息正常处理（_sync_one 收到）。
    """
    import registry

    synced = []
    async def fake_sync_one(gw, name, info):
        synced.append(name)
    monkeypatch.setattr(registry, "_sync_one", fake_sync_one)
    monkeypatch.setattr(registry, "_unmount_one", async_noop)

    n_created = {"n": 0}

    class FirstPubsub:
        """第 1 个 pubsub：listen 在订阅确认帧后抛 ConnectionError 模拟断连。

        注意：async generator 的 __anext__ 是只读的（CPython），不能直接
        赋值——用包装 generator 实现（外层 generator 转发内层，首帧后抛错）。
        """
        def __init__(self):
            self.ps = fake_redis.pubsub()
        async def subscribe(self, *channels):
            return await self.ps.subscribe(*channels)
        def listen(self):
            inner = self.ps.listen()
            async def wrapped():
                async for frame in inner:
                    if frame.get("type") == "subscribe":
                        # 交付订阅确认后模拟断连抛错（reconnect 前的最后一眼）
                        yield frame
                        raise ConnectionError("pubsub disconnected")
                    yield frame
            return wrapped()
        async def aclose(self):
            await self.ps.aclose()

    def make_pubsub():
        n_created["n"] += 1
        if n_created["n"] == 1:
            return FirstPubsub()
        return fake_redis.pubsub()

    class WrappedRedis:
        def pubsub(self):
            return make_pubsub()
        async def hgetall(self, key):
            return await fake_redis.hgetall(key)

    monkeypatch.setattr(registry, "get_redis", lambda: WrappedRedis())
    # 加速重连退避（默认 5s，测试等不起）
    monkeypatch.setattr(registry, "_PUBSUB_RETRY_DELAY", 0.1)

    gateway = FakeGW()
    task = asyncio.create_task(registry.watch_changes(gateway))
    try:
        # 发一条消息：pubsub #1 的 listen 首帧（subscribe 确认）正常返回，
        # watch_changes 的 async for 继续取帧时抛 ConnectionError → 重建
        await asyncio.wait_for(
            pubsub_send(fake_redis, "server:changed", json.dumps({"action": "add", "name": "s1"})),
            timeout=5,
        )
        # 等重建（pubsub #2 建立）——retry_delay 是 5s，等 10s 覆盖
        await asyncio.wait_for(wait_for(lambda: n_created["n"] == 2), timeout=15)
        assert n_created["n"] == 2, f"expected 2 pubsubs (1 dead + 1 rebuilt), got {n_created['n']}"
        # 重建后消息仍能正常处理
        await fake_redis.hset("servers:s2", mapping={"url": "http://s2", "status": "active"})
        await asyncio.wait_for(
            pubsub_send(fake_redis, "server:changed", json.dumps({"action": "add", "name": "s2"})),
            timeout=5,
        )
        await asyncio.wait_for(wait_for(lambda: "s2" in synced), timeout=5)
        assert synced == ["s2"], f"expected s2 synced after rebuild, got {synced}"
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


# ─── token:changed 失效通道 ────────────────────────────────────

async def test_watch_changes_token_changed_invalidates_cache(fake_redis, monkeypatch):
    """watch_changes 收到 token:changed → 调 invalidate_token_cache。

    watch_changes 内部 `from auth import invalidate_token_cache` 取的是
    auth 模块属性——monkeypatch 必须打在 auth 上（patch registry 无效）。
    """
    import json
    import registry
    import auth

    monkeypatch.setattr(registry, "_sync_one", async_noop)
    monkeypatch.setattr(registry, "_unmount_one", async_noop)
    calls = []
    monkeypatch.setattr(auth, "invalidate_token_cache", lambda h: calls.append(h))
    gateway = FakeGW()
    await presubscribed_pubsub(fake_redis, monkeypatch, registry)
    task = asyncio.create_task(registry.watch_changes(gateway))
    try:
        await asyncio.wait_for(pubsub_send(fake_redis, "token:changed", json.dumps({"token_hash": "abc123"})), timeout=5)
        await asyncio.wait_for(wait_for(lambda: len(calls) == 1), timeout=5)
        assert calls == ["abc123"]
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


async def test_watch_changes_server_changed_still_works(fake_redis, monkeypatch):
    """token:changed 分流后，server:changed 热加载不受影响。"""
    import json
    import registry

    synced = []
    async def fake_sync_one(gw, name, info):
        synced.append(name)
    monkeypatch.setattr(registry, "_sync_one", fake_sync_one)
    monkeypatch.setattr(registry, "_unmount_one", async_noop)

    gateway = FakeGW()
    await presubscribed_pubsub(fake_redis, monkeypatch, registry)
    task = asyncio.create_task(registry.watch_changes(gateway))
    try:
        await fake_redis.hset("servers:zabbix", mapping={"url": "http://zabbix", "status": "active"})
        await asyncio.wait_for(pubsub_send(fake_redis, "server:changed", json.dumps({"action": "update", "name": "zabbix"})), timeout=5)
        await asyncio.wait_for(wait_for(lambda: synced == ["zabbix"]), timeout=5)
        assert synced == ["zabbix"]
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass
