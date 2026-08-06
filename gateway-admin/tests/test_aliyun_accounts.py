"""阿里云账户管理 API 测试。"""
import json
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


@pytest.fixture
def no_probe(monkeypatch):
    """默认探活不真发：monkeypatch _probe 为成功。"""
    import api.aliyun_accounts as mod
    async def fake_probe(ak_id, ak_secret, region):
        return {"ok": True}
    monkeypatch.setattr(mod, "_probe", fake_probe)


async def test_create_account(no_probe, client, fake_redis, auth_headers):
    resp = client.post("/api/aliyun-accounts", json={
        "account_id": "prod-main",
        "description": "生产主账户",
        "access_key_id": "LTAI123",
        "access_key_secret": "sk-secret",
        "region": "cn-hangzhou",
    }, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["account_id"] == "prod-main"
    assert data["probe_error"] is None
    # Redis 已写入 + index + 发布
    assert (await fake_redis.hget("aliyndns:accounts:prod-main", "access_key_id")) == "LTAI123"
    assert await fake_redis.sismember("aliyndns:accounts:index", "prod-main")
    # 明文 secret 不进响应
    assert "access_key_secret" not in data


async def test_update_account(no_probe, client, fake_redis, auth_headers):
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s"}, headers=auth_headers)
    resp = client.put("/api/aliyun-accounts/acct1", json={"description": "新描述", "enabled": False},
                      headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert (await fake_redis.hget("aliyndns:accounts:acct1", "description")) == "新描述"


async def test_delete_account(no_probe, client, fake_redis, auth_headers):
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s"}, headers=auth_headers)
    resp = client.delete("/api/aliyun-accounts/acct1", headers=auth_headers)
    assert resp.status_code == 204
    assert not await fake_redis.sismember("aliyndns:accounts:index", "acct1")


def test_create_account_probe_failure_marks_probe_error(monkeypatch, client, fake_redis, auth_headers):
    import api.aliyun_accounts as mod
    async def bad_probe(ak_id, ak_secret, region):
        return {"ok": False, "error": "InvalidAccessKeyId.NotFound"}
    monkeypatch.setattr(mod, "_probe", bad_probe)
    resp = client.post("/api/aliyun-accounts", json={
        "account_id": "bad-key",
        "access_key_id": "LTAI-x", "access_key_secret": "sk",
    }, headers=auth_headers)
    assert resp.status_code == 201  # 探活失败不阻断添加
    assert resp.json()["probe_error"] == "InvalidAccessKeyId.NotFound"


def test_create_account_duplicate_rejected(no_probe, client, fake_redis, auth_headers):
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s"}, headers=auth_headers)
    resp = client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s"}, headers=auth_headers)
    assert resp.status_code == 422


def test_list_accounts_masks_secrets(no_probe, client, fake_redis, auth_headers):
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "description": "d",
        "access_key_id": "LTAI1234567890abcdef", "access_key_secret": "sk"}, headers=auth_headers)
    resp = client.get("/api/aliyun-accounts", headers=auth_headers)
    data = resp.json()
    assert len(data) == 1
    assert data[0]["account_id"] == "acct1"
    assert "LTAI1234567890abcdef" not in data[0]["access_key_masked"]
    assert "access_key_secret" not in data[0]


async def test_delete_account_cleans_token_perms(no_probe, client, fake_redis, auth_headers):
    """删除账户时清理所有 token 授权映射中的该账户引用（防僵尸授权）。"""
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s"}, headers=auth_headers)
    # 直接写 Redis 造授权映射（不经 API，模拟已有授权）
    import json as _json
    await fake_redis.hset("aliyndns:token_accounts:tokid_1", "acct1",
                          _json.dumps({"read": True, "write": False}))
    await fake_redis.hset("aliyndns:token_accounts:tokid_1", "acct2",
                          _json.dumps({"read": True, "write": False}))
    resp = client.delete("/api/aliyun-accounts/acct1", headers=auth_headers)
    assert resp.status_code == 204
    remaining = await fake_redis.hgetall("aliyndns:token_accounts:tokid_1")
    assert "acct1" not in remaining
    assert "acct2" in remaining


async def test_delete_account_resyncs_token_union(no_probe, client, fake_redis, auth_headers):
    """删除唯一授权账户后，gateway token 的 server 级 union 同步归零（防权限残留）。"""
    # 造 server + 账户 + token（aliyun-dns-mcp read+write）+ 授权 acct1(write)
    client.post("/api/servers", json={"name": "aliyun-dns-mcp",
                                      "url": "http://aliyun-dns-mcp:9054/mcp",
                                      "description": "dns"}, headers=auth_headers)
    client.post("/api/aliyun-accounts", json={
        "account_id": "acct1", "access_key_id": "a", "access_key_secret": "s",
        "probe": False}, headers=auth_headers)
    tok = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"aliyun-dns-mcp": {"read": True, "write": True}}},
        headers=auth_headers).json()["id"]
    client.put(f"/api/aliyun-perms/{tok}", json={
        "permissions": {"acct1": {"read": False, "write": True}}}, headers=auth_headers)
    token_hash = await fake_redis.get(f"token_id:{tok}")
    perms = json.loads((await fake_redis.hgetall(f"tokens:{token_hash}"))["permissions"])
    assert perms["aliyun-dns-mcp"] == {"read": True, "write": True}  # 前置：union 已开

    resp = client.delete("/api/aliyun-accounts/acct1", headers=auth_headers)
    assert resp.status_code == 204
    # 唯一授权被清空 → 授权映射 key 已删
    assert not await fake_redis.exists(f"aliyndns:token_accounts:{tok}")
    # server 级 union 归零，与 GET perms 返回 {} 一致
    perms = json.loads((await fake_redis.hgetall(f"tokens:{token_hash}"))["permissions"])
    assert perms["aliyun-dns-mcp"] == {"read": False, "write": False}


async def test_delete_account_union_keeps_other_accounts(no_probe, client, fake_redis, auth_headers):
    """删除多账户授权中的一个账户：保留其余授权并按其重算 union。"""
    client.post("/api/servers", json={"name": "aliyun-dns-mcp",
                                      "url": "http://aliyun-dns-mcp:9054/mcp",
                                      "description": "dns"}, headers=auth_headers)
    for aid in ("acct1", "acct2"):
        client.post("/api/aliyun-accounts", json={
            "account_id": aid, "access_key_id": "a", "access_key_secret": "s",
            "probe": False}, headers=auth_headers)
    tok = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"aliyun-dns-mcp": {"read": True, "write": True}}},
        headers=auth_headers).json()["id"]
    client.put(f"/api/aliyun-perms/{tok}", json={
        "permissions": {
            "acct1": {"read": False, "write": True},
            "acct2": {"read": True, "write": False},
        }}, headers=auth_headers)
    token_hash = await fake_redis.get(f"token_id:{tok}")
    perms = json.loads((await fake_redis.hgetall(f"tokens:{token_hash}"))["permissions"])
    assert perms["aliyun-dns-mcp"] == {"read": True, "write": True}

    resp = client.delete("/api/aliyun-accounts/acct1", headers=auth_headers)
    assert resp.status_code == 204
    # acct2 授权保留，acct1 引用已移除
    remaining = await fake_redis.hgetall(f"aliyndns:token_accounts:{tok}")
    assert "acct1" not in remaining
    assert json.loads(remaining["acct2"]) == {"read": True, "write": False}
    # union 按剩余 acct2 重算：write 降为 false
    perms = json.loads((await fake_redis.hgetall(f"tokens:{token_hash}"))["permissions"])
    assert perms["aliyun-dns-mcp"] == {"read": True, "write": False}
