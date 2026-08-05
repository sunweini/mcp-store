import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def test_create_server(client, fake_redis, auth_headers):
    resp = client.post("/api/servers", json={
        "name": "zabbix", "url": "http://localhost:8000/mcp",
        "description": "Zabbix MCP",
    }, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "zabbix"
    assert data["url"] == "http://localhost:8000/mcp"


def test_create_server_invalid_name_underscore(client, auth_headers):
    resp = client.post("/api/servers", json={
        "name": "my_server", "url": "http://x", "description": "",
    }, headers=auth_headers)
    assert resp.status_code == 422


def test_list_servers(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    resp = client.get("/api/servers", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "zabbix"


def test_delete_server(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    resp = client.delete("/api/servers/zabbix", headers=auth_headers)
    assert resp.status_code == 204
    # gone
    resp = client.get("/api/servers", headers=auth_headers)
    assert resp.json() == []


def test_unauth_rejected(client):
    assert client.get("/api/servers").status_code == 401


# ─── lifecycle（禁用/停用/启用）─────────────────────────────────

async def _seed_server(fake_redis, name="srv-a"):
    # fakeredis 是 async client，必须 await 才能真正写入种子数据
    await fake_redis.sadd("servers:active", name)
    await fake_redis.hset(f"servers:{name}", mapping={"name": name, "url": "http://x", "status": "active"})
    return name


async def test_lifecycle_disable_sets_status(fake_redis, client, auth_headers):
    name = await _seed_server(fake_redis)
    resp = client.post(f"/api/servers/{name}/lifecycle", json={"action": "disable"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    assert await fake_redis.hget(f"servers:{name}", "status") == "disabled"


async def test_lifecycle_stop_and_enable(fake_redis, client, auth_headers):
    name = await _seed_server(fake_redis)
    client.post(f"/api/servers/{name}/lifecycle", json={"action": "stop"}, headers=auth_headers)
    assert await fake_redis.hget(f"servers:{name}", "status") == "stopped"
    resp = client.post(f"/api/servers/{name}/lifecycle", json={"action": "enable"}, headers=auth_headers)
    assert resp.json()["status"] == "active"


async def test_lifecycle_invalid_action_422(fake_redis, client, auth_headers):
    name = await _seed_server(fake_redis)
    resp = client.post(f"/api/servers/{name}/lifecycle", json={"action": "boom"}, headers=auth_headers)
    assert resp.status_code == 422


async def test_lifecycle_missing_server_404(client, auth_headers):
    resp = client.post("/api/servers/nope/lifecycle", json={"action": "disable"}, headers=auth_headers)
    assert resp.status_code == 404
