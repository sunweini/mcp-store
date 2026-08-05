"""API Keys management API tests — CRUD, probe, usage, auth.

Probe tests never hit the real network: _probe_key accepts an injected
httpx.AsyncClient, so tests pass a MockTransport client. Other endpoints
monkeypatch api.keys._probe_key to skip probing entirely.
"""
import asyncio
import json
import time

import httpx
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def _seed(redis, provider="tavily", key_id="k1", **overrides):
    """Seed a key record directly in the fake Redis hash.

    TestClient runs the app in its own anyio portal loop; fakeredis commands
    from sync test code must run via asyncio.run so the coroutine is awaited
    (a fresh loop is fine — fakeredis keeps no loop-bound state).
    """
    rec = {
        "key": "tvly-secret-abc",
        "provider": provider,
        "enabled": True,
        "monthly_quota": 1000,
        "status": "active",
        "cooldown_until": None,
        "remaining": 1000,
        "last_used_at": None,
        "last_error": None,
    }
    rec.update(overrides)
    asyncio.run(redis.hset(f"search:keys:{provider}", key_id, json.dumps(rec)))


def _zadd(redis, name, members):
    asyncio.run(redis.zadd(name, members))


def test_list_keys_requires_auth(client):
    resp = client.get("/api/search-keys/tavily")
    assert resp.status_code == 401


def test_invalid_provider_returns_422(client, auth_headers):
    resp = client.get("/api/search-keys/notareal", headers=auth_headers)
    assert resp.status_code == 422


def test_add_key_success(client, fake_redis, auth_headers, monkeypatch):
    from api import keys as keys_module

    calls = []
    monkeypatch.setattr(keys_module, "_publish", _record(calls))
    monkeypatch.setattr(
        keys_module, "_probe_key", _fake_probe({"ok": True, "remaining": 1000})
    )
    resp = client.post(
        "/api/search-keys/tavily",
        json={"key": "tvly-xyz"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["key"] == "tvly-xyz"  # plaintext shown once
    assert data["key_id"].startswith("tavily_")
    assert data["status"] == "active"
    assert data["remaining"] == 1000
    # notify published so MCPs hot-reload
    assert ("upsert", "tavily", data["key_id"]) in calls

    # list returns mask, never plaintext
    lst = client.get("/api/search-keys/tavily", headers=auth_headers)
    assert lst.status_code == 200
    assert len(lst.json()) == 1
    rec = lst.json()[0]
    assert rec["key_id"] == data["key_id"]
    assert "key" not in rec
    assert rec["key_masked"] == "tvly…"  # len("tvly-xyz")<=12 -> 仅前4+省略号


def test_publish_failure_does_not_block_write(client, fake_redis, auth_headers, monkeypatch):
    """publish 失败只记 warning：Redis 已写入是主操作，通知是尽力而为。

    阻断会让前端看到 500 而重试 → 重复建 key（数据已在库）。
    只让 redis.publish 抛错、保留真实 _publish 的 try/except 在链路上，
    验证的是真实错误处理路径（monkeypatch 整个 _publish 会连兜底一起换掉）。
    """
    from api import keys as keys_module

    async def boom_publish(*a, **kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr(fake_redis, "publish", boom_publish)
    monkeypatch.setattr(keys_module, "_probe_key", _fake_probe({"ok": True}))
    resp = client.post(
        "/api/search-keys/tavily",
        json={"key": "tvly-xyz"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    key_id = resp.json()["key_id"]
    assert key_id.startswith("tavily_")

    # 同样的 publish 故障下 PUT 也成功
    resp = client.put(
        f"/api/search-keys/tavily/{key_id}",
        json={"enabled": False},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_publish_failure_does_not_block_delete(client, fake_redis, auth_headers, monkeypatch):
    from api import keys as keys_module

    async def boom_publish(*a, **kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr(fake_redis, "publish", boom_publish)
    _seed(fake_redis)
    resp = client.delete("/api/search-keys/tavily/k1", headers=auth_headers)
    assert resp.status_code == 204
    assert not asyncio.run(fake_redis.hexists("search:keys:tavily", "k1"))


def test_list_keys_masks_plaintext(client, fake_redis, auth_headers):
    _seed(fake_redis)
    resp = client.get("/api/search-keys/tavily", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["key_id"] == "k1"
    assert "key" not in data[0]
    assert data[0]["key_masked"] == "tvly…-abc"  # key "tvly-secret-abc" 前4后4


def test_add_key_probe_failure_sets_invalid(client, fake_redis, auth_headers, monkeypatch):
    from api import keys as keys_module

    monkeypatch.setattr(
        keys_module, "_probe_key",
        _fake_probe({"ok": False, "error": "probe HTTP 401"}),
    )
    resp = client.post(
        "/api/search-keys/serpapi",
        json={"key": "serp-xyz"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "invalid"
    assert resp.json()["last_error"] == "probe HTTP 401"


def test_add_key_blank_rejected(client, fake_redis, auth_headers, monkeypatch):
    from api import keys as keys_module

    monkeypatch.setattr(keys_module, "_probe_key", _fake_probe({"ok": True}))
    resp = client.post(
        "/api/search-keys/tavily",
        json={"key": "   "},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_add_key_zero_quota_rejected(client, fake_redis, auth_headers, monkeypatch):
    """quota=0 会被 MCP 侧回退成默认值（0/缺失语义一致），拒绝以免
    管理界面显示与 MCP 实际行为分裂；负数同理（Field ge=1 拒绝）。"""
    from api import keys as keys_module

    monkeypatch.setattr(keys_module, "_probe_key", _fake_probe({"ok": True}))
    resp = client.post(
        "/api/search-keys/tavily",
        json={"key": "tvly-xyz", "monthly_quota": 0},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    resp = client.post(
        "/api/search-keys/tavily",
        json={"key": "tvly-xyz", "monthly_quota": -5},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_update_key(client, fake_redis, auth_headers, monkeypatch):
    from api import keys as keys_module

    calls = []
    monkeypatch.setattr(keys_module, "_publish", _record(calls))
    _seed(fake_redis)
    resp = client.put(
        "/api/search-keys/tavily/k1",
        json={"enabled": False, "monthly_quota": 500},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"key_id": "k1", "enabled": False, "monthly_quota": 500}
    stored = json.loads(asyncio.run(fake_redis.hget("search:keys:tavily", "k1")))
    assert stored["enabled"] is False
    assert stored["monthly_quota"] == 500
    assert ("upsert", "tavily", "k1") in calls


def test_update_key_not_found(client, fake_redis, auth_headers):
    resp = client.put(
        "/api/search-keys/tavily/ghost",
        json={"enabled": False},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_update_key_corrupt_payload_404(client, fake_redis, auth_headers):
    """损坏 payload 当不存在处理（与 list 的脏数据策略一致），不 500。"""
    _seed(fake_redis)
    asyncio.run(fake_redis.hset("search:keys:tavily", "k1", "{not-json"))
    resp = client.put(
        "/api/search-keys/tavily/k1",
        json={"enabled": False},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_delete_key_removes_hash_and_usage(client, fake_redis, auth_headers, monkeypatch):
    from api import keys as keys_module

    calls = []
    monkeypatch.setattr(keys_module, "_publish", _record(calls))
    _seed(fake_redis)
    # 与 MCP 侧一致：zadd(member=str(now), score=now)
    _zadd(fake_redis, "search:usage:tavily:k1", {"1000000": 1000000})
    resp = client.delete("/api/search-keys/tavily/k1", headers=auth_headers)
    assert resp.status_code == 204
    assert not asyncio.run(fake_redis.hexists("search:keys:tavily", "k1"))
    assert not asyncio.run(fake_redis.exists("search:usage:tavily:k1"))
    assert ("delete", "tavily", "k1") in calls


def test_delete_key_not_found(client, fake_redis, auth_headers):
    resp = client.delete("/api/search-keys/tavily/ghost", headers=auth_headers)
    assert resp.status_code == 404


def test_usage_report(client, fake_redis, auth_headers):
    now = int(time.time())
    _seed(fake_redis, key_id="k1", monthly_quota=1000)
    _seed(fake_redis, key_id="k2", monthly_quota=100, remaining=None)
    # k2: 2 local calls this month -> remaining falls back to quota - used
    _zadd(fake_redis, "search:usage:tavily:k2",
          {str(now - 100): now - 100, str(now): now})
    # 上个月成员（score 在窗口起点之前），不应计入当月
    _zadd(fake_redis, "search:usage:tavily:k2", {"1000000": 1000000})
    resp = client.get("/api/search-keys/tavily/usage", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider"] == "tavily"
    by_id = {k["key_id"]: k for k in data["keys"]}
    # k1 has official remaining=1000
    assert by_id["k1"]["remaining"] == 1000
    assert by_id["k1"]["month_usage"] == 0
    # k2: 2 members this month, quota 100 -> remaining 98
    assert by_id["k2"]["month_usage"] == 2
    assert by_id["k2"]["remaining"] == 98
    assert by_id["k2"]["ratio"] == pytest.approx(0.98)


async def test_probe_tavily_ok_and_remaining():
    """usage 新 schema：account.plan_limit / account.plan_usage（与 calibrate 同口径）。"""
    from api.keys import _probe_key

    def handler(request):
        if request.url.path == "/usage":
            return httpx.Response(200, json={
                "account": {"plan_usage": 120, "plan_limit": 1000},
            })
        return httpx.Response(200, json={"answer": "ping"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await _probe_key("tavily", "tvly-x", client=c)
    assert res["ok"] is True
    assert res["remaining"] == 880


async def test_probe_tavily_usage_overage_clamps_to_zero():
    """plan_usage > plan_limit（超用套餐）→ remaining clamp 0，不产生负数。"""
    from api.keys import _probe_key

    def handler(request):
        if request.url.path == "/usage":
            return httpx.Response(200, json={
                "account": {"plan_usage": 1200, "plan_limit": 1000},
            })
        return httpx.Response(200, json={"answer": "ping"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await _probe_key("tavily", "tvly-x", client=c)
    assert res["ok"] is True
    assert res["remaining"] == 0


async def test_probe_tavily_usage_missing_account_fields_remaining_none():
    """usage 响应缺 account.plan_* 字段（schema 漂移/旧 schema）→ remaining=None，
    但探活本身成功（查询已验证 key 有效，余量取不到不应算失败）。"""
    from api.keys import _probe_key

    def handler(request):
        if request.url.path == "/usage":
            # 旧 schema monthly_usage.*：新代码不认，remaining 保持 None
            return httpx.Response(200, json={
                "monthly_usage": {"current_usage": 120, "max_usage": 1000},
            })
        return httpx.Response(200, json={"answer": "ping"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await _probe_key("tavily", "tvly-x", client=c)
    assert res["ok"] is True
    assert res["remaining"] is None


async def test_probe_tavily_unauthorized():
    from api.keys import _probe_key

    async def handler(request):
        return httpx.Response(401, json={"error": "invalid api key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await _probe_key("tavily", "tvly-bad", client=c)
    assert res["ok"] is False
    assert "401" in res["error"]


async def test_probe_brave_ok():
    from api.keys import _probe_key

    async def handler(request):
        return httpx.Response(200, json={"web": {"results": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await _probe_key("brave", "bsa-x", client=c)
    assert res["ok"] is True


async def test_probe_brave_uses_search_proxy_env(monkeypatch):
    """client=None（生产路径）时 brave 探活必须走 SEARCH_PROXY 代理。

    生产网络 api.search.brave.com 直连不通，仅 brave 需要代理（tavily/
    serpapi 直连通）。断言 proxy 关键字从 env 传入 httpx.AsyncClient。

    注意 httpx 0.28 的坑：proxy 与 transport 同时传时 proxy 覆盖
    transport（真实连接优先），测试注入 MockTransport 时必须丢弃 proxy
    ——这也是生产代码不同时传两者的原因。
    """
    import os
    from api import keys as keys_module
    from api.keys import _probe_key

    monkeypatch.setenv("SEARCH_PROXY", "http://10.16.12.12:7890")
    captured = {}
    real_cls = httpx.AsyncClient

    class _FakeClient(real_cls):
        def __init__(self, *args, **kwargs):
            captured["proxy"] = kwargs.get("proxy")
            kwargs.pop("proxy", None)  # 见 docstring：proxy 会覆盖 transport
            kwargs["transport"] = httpx.MockTransport(
                lambda request: httpx.Response(200, json={"web": {"results": []}}))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(keys_module.httpx, "AsyncClient", _FakeClient)
    res = await _probe_key("brave", "bsa-x")
    assert res["ok"] is True
    assert captured["proxy"] == "http://10.16.12.12:7890"


async def test_probe_brave_no_proxy_env_means_direct(monkeypatch):
    """未配 SEARCH_PROXY 时 proxy=None（直连），且不影响 tavily 探活。"""
    import os
    from api import keys as keys_module
    from api.keys import _probe_key

    monkeypatch.delenv("SEARCH_PROXY", raising=False)
    captured = {}
    real_cls = httpx.AsyncClient

    class _FakeClient(real_cls):
        def __init__(self, *args, **kwargs):
            captured["proxy"] = kwargs.get("proxy")
            kwargs["transport"] = httpx.MockTransport(
                lambda request: httpx.Response(200, json={"answer": "ping"}))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(keys_module.httpx, "AsyncClient", _FakeClient)
    res = await _probe_key("tavily", "tvly-x")
    assert res["ok"] is True
    assert captured["proxy"] is None


async def test_probe_serpapi_ok():
    from api.keys import _probe_key

    async def handler(request):
        return httpx.Response(200, json={"organic_results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await _probe_key("serpapi", "serp-x", client=c)
    assert res["ok"] is True


async def test_probe_serpapi_error_body():
    from api.keys import _probe_key

    async def handler(request):
        return httpx.Response(200, json={"error": "api limit reached"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await _probe_key("serpapi", "serp-x", client=c)
    assert res["ok"] is False


async def test_probe_serpapi_empty_body_no_json_error():
    """Non-200 + empty body must not raise JSONDecodeError (brief gotcha #1)."""
    from api.keys import _probe_key

    async def handler(request):
        return httpx.Response(403, content=b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await _probe_key("serpapi", "serp-bad", client=c)
    assert res["ok"] is False
    assert "403" in res["error"]


async def test_probe_network_error():
    from api.keys import _probe_key

    async def handler(request):
        raise httpx.ConnectError("boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await _probe_key("tavily", "tvly-x", client=c)
    assert res["ok"] is False
    assert "boom" in res["error"]


async def test_publish_roundtrip(fake_redis):
    """_publish actually PUBLISHes on search:keys:channel (notify MCP hot-reload)."""
    from api.keys import _publish

    pubsub = fake_redis.pubsub()
    await pubsub.subscribe("search:keys:channel")
    await pubsub.get_message(timeout=0.5)  # drain subscribe ack
    await _publish("upsert", "tavily", "k1")
    msg = await pubsub.get_message(timeout=0.5)
    assert msg is not None and msg["type"] == "message"
    assert json.loads(msg["data"]) == {
        "provider": "tavily", "action": "upsert", "key_id": "k1",
    }
    await pubsub.aclose()


def _fake_probe(result):
    async def probe(provider, key, client=None):
        return result
    return probe


def _record(calls):
    async def publish(action, provider, key_id):
        calls.append((action, provider, key_id))
    return publish


def _raise(exc):
    async def publish(action, provider, key_id):
        raise exc
    return publish
