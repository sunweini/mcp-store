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
