import json
import pytest
from auth import create_jwt
from api.tokens import hash_token, generate_token


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def test_generate_token_format():
    t = generate_token()
    assert t.startswith("tok_")
    assert len(t) > 16


def test_hash_token_sha256():
    h = hash_token("tok_abc")
    assert len(h) == 64  # sha256 hex


def test_create_token_returns_plaintext_once(client, fake_redis, auth_headers):
    # prereq: a server must exist for permission validation
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    resp = client.post("/api/tokens", json={
        "name": "zabbix-readonly",
        "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["token"].startswith("tok_")  # plaintext shown once
    assert data["name"] == "zabbix-readonly"


def test_list_tokens_returns_mask_not_plaintext(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    resp = client.get("/api/tokens", headers=auth_headers)
    data = resp.json()
    assert len(data) == 1
    assert data[0]["token_masked"].endswith("****")
    assert "token" not in data[0]  # no plaintext


def test_create_token_unknown_server_rejected(client, fake_redis, auth_headers):
    resp = client.post("/api/tokens", json={
        "name": "bad", "permissions": {"ghost": {"read": True, "write": False}},
    }, headers=auth_headers)
    assert resp.status_code == 422


def test_delete_token(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    r = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    tok_id = r.json()["id"]
    resp = client.delete(f"/api/tokens/{tok_id}", headers=auth_headers)
    assert resp.status_code == 204


# ─── token:changed 失效通知 ────────────────────────────────────

def test_create_token_publishes_token_changed(client, fake_redis, auth_headers, monkeypatch):
    """create_token 成功后 publish 一次 token:changed（缓存即时失效）。

    monkeypatch 放在 server 创建之后：避免把 server:changed 也计入。
    """
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    publishes = []
    async def counting_publish(channel, message):
        publishes.append((channel, message))
    monkeypatch.setattr(fake_redis, "publish", counting_publish)
    resp = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    assert resp.status_code == 201
    assert len(publishes) == 1
    channel, message = publishes[0]
    assert channel == "token:changed"
    payload = json.loads(message)
    assert len(payload["token_hash"]) == 64  # sha256 hex


def test_delete_token_publishes_token_changed(client, fake_redis, auth_headers, monkeypatch):
    """delete_token 成功后 publish 一次 token:changed（吊销即时生效）。"""
    publishes = []
    async def counting_publish(channel, message):
        publishes.append((channel, message))
    monkeypatch.setattr(fake_redis, "publish", counting_publish)
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    r = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    publishes.clear()
    tok_id = r.json()["id"]
    resp = client.delete(f"/api/tokens/{tok_id}", headers=auth_headers)
    assert resp.status_code == 204
    assert len(publishes) == 1
    channel, message = publishes[0]
    assert channel == "token:changed"
    assert len(json.loads(message)["token_hash"]) == 64


# ─── PUT /api/tokens/{id} — 更新 server 级权限 ──────────────────────


def test_update_token_permissions(client, fake_redis, auth_headers):
    """PUT 更新 token 的 server 级 read/write 权限。"""
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    client.post("/api/servers", json={"name": "tavily", "url": "http://y", "description": ""},
                headers=auth_headers)
    r = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    tok_id = r.json()["id"]

    resp = client.put(f"/api/tokens/{tok_id}", json={
        "permissions": {"zabbix": {"read": True, "write": True}, "tavily": {"read": True, "write": False}},
    }, headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["permissions"]["zabbix"] == {"read": True, "write": True}
    assert data["permissions"]["tavily"] == {"read": True, "write": False}


def test_update_token_unknown_server_rejected(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    r = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    tok_id = r.json()["id"]

    resp = client.put(f"/api/tokens/{tok_id}", json={
        "permissions": {"ghost": {"read": True, "write": False}},
    }, headers=auth_headers)
    assert resp.status_code == 422


def test_update_token_not_found(client, fake_redis, auth_headers):
    resp = client.put("/api/tokens/tokid_missing", json={
        "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    assert resp.status_code == 404


def test_update_token_revoke_all_persists_false(client, fake_redis, auth_headers):
    """PUT 发送 read=false,write=false 的 server 应存为取消（条目在但全 false）。

    修复「编辑MCP权限」取消权限不生效的回归：前端若过滤掉全 false 的
    server 不再发送，后端会因「只更新出现的 server」而保留旧值。后端层面
    正确行为是：收到全 false 就写成 false 条目，让 check_permission 拒绝。
    """
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    r = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": True}},
    }, headers=auth_headers)
    tok_id = r.json()["id"]

    resp = client.put(f"/api/tokens/{tok_id}", json={
        "permissions": {"zabbix": {"read": False, "write": False}},
    }, headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["permissions"]["zabbix"] == {"read": False, "write": False}


def test_update_token_does_not_overwrite_aliyun_dns(client, fake_redis, auth_headers):
    """MCP 级编辑不覆盖 aliyun-dns-mcp 的粗闸（账户授权矩阵权威）。

    创建时把 aliyun-dns-mcp 设为 write=true（模拟账户授权 union 结果），
    然后用 PUT 尝试把 aliyun-dns-mcp 改成 write=false——应被忽略，保持
    write=true；同时正常更新其它 server（tavily）。
    """
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    client.post("/api/servers", json={"name": "aliyun-dns-mcp", "url": "http://a", "description": ""},
                headers=auth_headers)
    client.post("/api/servers", json={"name": "tavily", "url": "http://y", "description": ""},
                headers=auth_headers)
    r = client.post("/api/tokens", json={
        "name": "ro",
        "permissions": {"aliyun-dns-mcp": {"read": True, "write": True},
                        "tavily": {"read": True, "write": False}},
    }, headers=auth_headers)
    tok_id = r.json()["id"]

    resp = client.put(f"/api/tokens/{tok_id}", json={
        "permissions": {"aliyun-dns-mcp": {"read": False, "write": False}, "tavily": {"read": True, "write": True}},
    }, headers=auth_headers)

    assert resp.status_code == 200
    # aliyun-dns-mcp 保持不变（write=true 未被覆盖），tavily 已更新为 write=true
    assert resp.json()["permissions"]["aliyun-dns-mcp"] == {"read": True, "write": True}
    assert resp.json()["permissions"]["tavily"] == {"read": True, "write": True}


def test_update_token_publishes_token_changed(client, fake_redis, auth_headers, monkeypatch):
    """PUT 成功后 publish 一次 token:changed（缓存即时失效）。"""
    publishes = []
    async def counting_publish(channel, message):
        publishes.append((channel, message))
    monkeypatch.setattr(fake_redis, "publish", counting_publish)
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    r = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    publishes.clear()
    tok_id = r.json()["id"]
    resp = client.put(f"/api/tokens/{tok_id}", json={
        "permissions": {"zabbix": {"read": True, "write": True}},
    }, headers=auth_headers)
    assert resp.status_code == 200
    assert len(publishes) == 1
    channel, message = publishes[0]
    assert channel == "token:changed"
    assert len(json.loads(message)["token_hash"]) == 64
