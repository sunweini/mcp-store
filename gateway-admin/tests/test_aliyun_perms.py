"""token×账户授权矩阵 API 测试（含 union 同步 gateway token）。"""
import json
import pytest
from auth import create_jwt
from api.tokens import hash_token


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def _seed_server(client, auth_headers, name="aliyun-dns-mcp"):
    client.post("/api/servers", json={"name": name, "url": "http://aliyun-dns-mcp:9054/mcp",
                                      "description": "dns"}, headers=auth_headers)


def _seed_account(client, auth_headers, account_id="acct1"):
    client.post("/api/aliyun-accounts", json={
        "account_id": account_id, "access_key_id": "a", "access_key_secret": "s",
        "probe": False}, headers=auth_headers)


def _seed_token(client, auth_headers, name="ro") -> str:
    resp = client.post("/api/tokens", json={
        "name": name, "permissions": {"aliyun-dns-mcp": {"read": True, "write": False}}},
        headers=auth_headers)
    return resp.json()["id"]


def test_get_perms_empty(client, fake_redis, auth_headers):
    _seed_server(client, auth_headers)
    token_id = _seed_token(client, auth_headers)
    resp = client.get(f"/api/aliyun-perms/{token_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["permissions"] == {}


def test_put_perms_write_implies_read(client, fake_redis, auth_headers):
    _seed_server(client, auth_headers)
    _seed_account(client, auth_headers)
    token_id = _seed_token(client, auth_headers)
    resp = client.put(f"/api/aliyun-perms/{token_id}", json={
        "permissions": {"acct1": {"read": False, "write": True}}}, headers=auth_headers)
    assert resp.status_code == 200
    # write ⇒ read 强制
    assert resp.json()["permissions"]["acct1"] == {"read": True, "write": True}


def test_put_perms_unknown_account_rejected(client, fake_redis, auth_headers):
    _seed_server(client, auth_headers)
    _seed_account(client, auth_headers)
    token_id = _seed_token(client, auth_headers)
    resp = client.put(f"/api/aliyun-perms/{token_id}", json={
        "permissions": {"ghost": {"read": True, "write": False}}}, headers=auth_headers)
    assert resp.status_code == 422


async def test_put_perms_syncs_union_to_gateway_token(client, fake_redis, auth_headers):
    _seed_server(client, auth_headers)
    _seed_account(client, auth_headers, "acct1")
    _seed_account(client, auth_headers, "acct2")
    token_id = _seed_token(client, auth_headers)
    # 原 token：aliyun-dns-mcp read+write（可见性粗闸先开）
    client.put(f"/api/aliyun-perms/{token_id}", json={
        "permissions": {
            "acct1": {"read": True, "write": False},
            "acct2": {"read": True, "write": True},
        }}, headers=auth_headers)
    # 找 token hash 并验证 union
    token_hash = await fake_redis.get(f"token_id:{token_id}")
    data = await fake_redis.hgetall(f"tokens:{token_hash}")
    perms = json.loads(data["permissions"])
    assert perms["aliyun-dns-mcp"] == {"read": True, "write": True}  # 任一账户有 write
    # 授权映射已写
    raw = await fake_redis.hget(f"aliyndns:token_accounts:{token_id}", "acct2")
    assert json.loads(raw) == {"read": True, "write": True}


async def test_put_perms_clear_all_removes_mapping(client, fake_redis, auth_headers):
    _seed_server(client, auth_headers)
    _seed_account(client, auth_headers)
    token_id = _seed_token(client, auth_headers)
    client.put(f"/api/aliyun-perms/{token_id}", json={
        "permissions": {"acct1": {"read": True, "write": True}}}, headers=auth_headers)
    resp = client.put(f"/api/aliyun-perms/{token_id}", json={"permissions": {}}, headers=auth_headers)
    assert resp.status_code == 200
    assert not await fake_redis.exists(f"aliyndns:token_accounts:{token_id}")
    token_hash = await fake_redis.get(f"token_id:{token_id}")
    perms = json.loads((await fake_redis.hgetall(f"tokens:{token_hash}"))["permissions"])
    assert perms["aliyun-dns-mcp"] == {"read": False, "write": False}  # 全清 → 无权限
