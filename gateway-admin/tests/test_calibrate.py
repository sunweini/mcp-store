"""官方用量校准测试 — fetch 解析、Redis 更新语义、路由鉴权。

所有网络均 mock（fetcher 注入 / httpx.MockTransport），Redis 用 fake_redis
fixture，绝不触达真实 provider 接口。路由测试 monkeypatch 模块级 fetcher
（calibrate_provider 运行时按模块全局名查找，patch 生效）。
"""
import asyncio
import json

import httpx
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def _make_rec(provider, key_id, **overrides):
    rec = {
        "key": f"{provider}-secret-{key_id}",
        "provider": provider,
        "enabled": True,
        "monthly_quota": 1000,
        "status": "active",
        "cooldown_until": None,
        "remaining": 500,
        "last_used_at": None,
        "last_error": None,
    }
    rec.update(overrides)
    return rec


def _seed(redis, provider, key_id, **overrides):
    """sync 测试（TestClient 路由测试）用：asyncio.run 起新 loop 写 fakeredis。

    TestClient 在自己的 anyio portal loop 里跑 app，sync 测试代码对
    fakeredis 的命令只能 asyncio.run（fakeredis 不绑 loop，新 loop 安全）。
    """
    asyncio.run(redis.hset(f"search:keys:{provider}", key_id,
                           json.dumps(_make_rec(provider, key_id, **overrides))))


async def _seed_async(redis, provider, key_id, **overrides):
    """async 测试用：已有 running loop，asyncio.run 会报错，直接 await。"""
    await redis.hset(f"search:keys:{provider}", key_id,
                     json.dumps(_make_rec(provider, key_id, **overrides)))


# ── fetch_tavily_usage ──────────────────────────────────────────


async def test_fetch_tavily_usage_parses_plan_fields():
    """实测响应格式：account.plan_limit / account.plan_usage（非 key.usage）。"""
    from calibrate import fetch_tavily_usage

    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={
            "key": {"usage": 5},
            "account": {"plan_usage": 112, "plan_limit": 1000, "plan": "pro"},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await fetch_tavily_usage("tvly-x", client=c)
    assert res == {"quota": 1000, "remaining": 888}
    assert captured["auth"] == "Bearer tvly-x"  # Bearer 鉴权走 header


async def test_fetch_tavily_usage_overage_clamps_to_zero():
    """usage > limit（超用套餐）时 remaining clamp 0，不产生负数。"""
    from calibrate import fetch_tavily_usage

    def handler(request):
        return httpx.Response(200, json={
            "account": {"plan_usage": 1200, "plan_limit": 1000},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await fetch_tavily_usage("tvly-x", client=c)
    assert res == {"quota": 1000, "remaining": 0}


async def test_fetch_tavily_usage_non200_returns_none():
    from calibrate import fetch_tavily_usage

    async def handler(request):
        return httpx.Response(401, json={"error": "invalid api key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await fetch_tavily_usage("tvly-bad", client=c) is None


async def test_fetch_tavily_usage_bad_schema_returns_none():
    """缺 account 字段 → None（拒绝写入猜测值）。"""
    from calibrate import fetch_tavily_usage

    async def handler(request):
        return httpx.Response(200, json={"key": {"usage": 5}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await fetch_tavily_usage("tvly-x", client=c) is None


async def test_fetch_tavily_usage_network_error_returns_none():
    from calibrate import fetch_tavily_usage

    async def handler(request):
        raise httpx.ConnectError("boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await fetch_tavily_usage("tvly-x", client=c) is None


# ── fetch_serpapi_usage ─────────────────────────────────────────


async def test_fetch_serpapi_usage_parses():
    from calibrate import fetch_serpapi_usage

    captured = {}

    def handler(request):
        captured["api_key"] = request.url.params.get("api_key")
        return httpx.Response(200, json={
            "email": "a@b.c", "searches_per_month": 250, "plan_searches_left": 237,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await fetch_serpapi_usage("serp-x", client=c)
    assert res == {"quota": 250, "remaining": 237}
    assert captured["api_key"] == "serp-x"  # SerpAPI 只支持 query param 鉴权


async def test_fetch_serpapi_usage_error_body_returns_none():
    """serpapi 把错误放进 200 body（怪癖同 /search）→ 视为失败。"""
    from calibrate import fetch_serpapi_usage

    async def handler(request):
        return httpx.Response(200, json={"error": "invalid api key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await fetch_serpapi_usage("serp-bad", client=c) is None


async def test_fetch_serpapi_usage_empty_body_no_json_error():
    """非 200 + 空 body 不能抛 JSONDecodeError（探活同款坑）。"""
    from calibrate import fetch_serpapi_usage

    async def handler(request):
        return httpx.Response(403, content=b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await fetch_serpapi_usage("serp-bad", client=c) is None


# ── calibrate_provider ──────────────────────────────────────────


async def test_calibrate_provider_updates_redis(fake_redis, monkeypatch):
    """成功校准：monthly_quota + remaining 覆写，其余字段不动，且 publish 通知。"""
    import calibrate as cal
    from api import keys as keys_module

    async def fake_fetch(key, client=None):
        return {"quota": 2000, "remaining": 1234}

    monkeypatch.setattr(cal, "fetch_tavily_usage", fake_fetch)

    publish_calls = []

    async def record_publish(action, provider, key_id):
        publish_calls.append((action, provider, key_id))

    monkeypatch.setattr(keys_module, "_publish", record_publish)

    await _seed_async(fake_redis, "tavily", "k1")
    await _seed_async(fake_redis, "tavily", "k2", monthly_quota=50, remaining=10)

    summary = await cal.calibrate_provider("tavily")
    assert summary["provider"] == "tavily"
    assert summary["supported"] is True
    by_id = {k["key_id"]: k for k in summary["keys"]}
    assert by_id["k1"] == {"key_id": "k1", "quota": 2000, "remaining": 1234}
    assert by_id["k2"] == {"key_id": "k2", "quota": 2000, "remaining": 1234}

    stored = json.loads(await fake_redis.hget("search:keys:tavily", "k1"))
    assert stored["monthly_quota"] == 2000
    assert stored["remaining"] == 1234
    # 只覆写配额字段：其余状态不动
    assert stored["enabled"] is True
    assert stored["status"] == "active"
    assert stored["key"] == "tavily-secret-k1"

    # MCP 按 remaining 挑 key：配额变了必须 publish 触发热重载
    assert publish_calls == [("calibrate", "tavily", "k2")]


async def test_calibrate_provider_fetch_failure_keeps_record(fake_redis, monkeypatch):
    """官方拉取失败 → 记 error、绝不改存储值（不破坏现有记录）。"""
    import calibrate as cal

    async def fail_fetch(key, client=None):
        return None

    monkeypatch.setattr(cal, "fetch_tavily_usage", fail_fetch)
    await _seed_async(fake_redis, "tavily", "k1", monthly_quota=777, remaining=42)

    summary = await cal.calibrate_provider("tavily")
    assert summary["keys"] == [
        {"key_id": "k1", "error": "official usage unavailable"},
    ]
    stored = json.loads(await fake_redis.hget("search:keys:tavily", "k1"))
    assert stored["monthly_quota"] == 777
    assert stored["remaining"] == 42


async def test_calibrate_provider_corrupt_record_skipped(fake_redis, monkeypatch):
    """损坏记录跳过并记 error，其余 key 正常校准（不让整批 500）。"""
    import calibrate as cal

    async def fake_fetch(key, client=None):
        return {"quota": 100, "remaining": 99}

    monkeypatch.setattr(cal, "fetch_tavily_usage", fake_fetch)
    await _seed_async(fake_redis, "tavily", "k1")
    await fake_redis.hset("search:keys:tavily", "bad", "{not-json")

    summary = await cal.calibrate_provider("tavily")
    by_id = {k["key_id"]: k for k in summary["keys"]}
    assert by_id["bad"]["error"] == "corrupt record"
    assert by_id["k1"]["quota"] == 100


async def test_calibrate_brave_unsupported_and_untouched(fake_redis):
    """brave 无公开用量接口：supported=false，记录完全不碰。"""
    import calibrate as cal

    await _seed_async(fake_redis, "brave", "b1", monthly_quota=2000, remaining=1500)
    summary = await cal.calibrate_provider("brave")
    assert summary == {"provider": "brave", "supported": False, "keys": []}
    stored = json.loads(await fake_redis.hget("search:keys:brave", "b1"))
    assert stored["monthly_quota"] == 2000
    assert stored["remaining"] == 1500


# ── POST /api/search-keys/calibrate 路由 ────────────────────────


def test_calibrate_route_requires_auth(client):
    resp = client.post("/api/search-keys/calibrate")
    assert resp.status_code == 401


def test_calibrate_route_returns_all_providers(client, fake_redis, auth_headers,
                                               monkeypatch):
    """三源摘要列表：tavily/serpapi 校准，brave supported=false 且不碰值。"""
    import calibrate as cal

    async def tavily_fetch(key, client=None):
        return {"quota": 1000, "remaining": 888}

    async def serpapi_fetch(key, client=None):
        return {"quota": 250, "remaining": 237}

    monkeypatch.setattr(cal, "fetch_tavily_usage", tavily_fetch)
    monkeypatch.setattr(cal, "fetch_serpapi_usage", serpapi_fetch)

    _seed(fake_redis, "tavily", "t1")
    _seed(fake_redis, "serpapi", "s1", monthly_quota=100, remaining=50)
    _seed(fake_redis, "brave", "b1", monthly_quota=2000, remaining=1500)

    resp = client.post("/api/search-keys/calibrate", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    by_provider = {s["provider"]: s for s in data}
    assert set(by_provider) == {"tavily", "brave", "serpapi"}

    assert by_provider["tavily"]["supported"] is True
    assert by_provider["tavily"]["keys"] == [
        {"key_id": "t1", "quota": 1000, "remaining": 888},
    ]
    assert by_provider["serpapi"]["keys"] == [
        {"key_id": "s1", "quota": 250, "remaining": 237},
    ]
    assert by_provider["brave"]["supported"] is False

    # brave 记录原样；tavily/serpapi 已覆写
    brave = json.loads(asyncio.run(fake_redis.hget("search:keys:brave", "b1")))
    assert brave["monthly_quota"] == 2000 and brave["remaining"] == 1500
    tavily = json.loads(asyncio.run(fake_redis.hget("search:keys:tavily", "t1")))
    assert tavily["monthly_quota"] == 1000 and tavily["remaining"] == 888
