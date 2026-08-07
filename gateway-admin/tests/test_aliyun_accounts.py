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


async def test_probe_error_sanitized_when_contains_url(monkeypatch, client, fake_redis, auth_headers):
    """I1 回归：网络错误消息含完整 URL query（AccessKeyId 明文）时必须裁剪。

    probe_error 会落 Redis + 列表接口返回 + 前端展示——任何带凭证的内容
    都是泄漏。SDK Client 构造抛 ConnectionError 走真实 _probe 的 except
    路径（monkeypatch _probe 本身会绕过内部裁剪，是假回归）。
    """
    import api.aliyun_accounts as mod
    import alibabacloud_alidns20150109.client as sdk_client_mod
    url_msg = ("HTTPSConnectionPool(host='alidns.cn-hangzhou.aliyuncs.com', port=443): "
               "Max retries exceeded ...: GET https://alidns.cn-hangzhou.aliyuncs.com/?"
               "AccessKeyId=LTAI5t-demo-secret-value&Signature=abc&version=2015-01-09")
    monkeypatch.setattr(sdk_client_mod.Client, "__init__",
                        lambda self, config: (_ for _ in ()).throw(ConnectionError(url_msg)))
    resp = client.post("/api/aliyun-accounts", json={
        "account_id": "net-fail",
        "access_key_id": "LTAI-x", "access_key_secret": "sk",
    }, headers=auth_headers)
    assert resp.status_code == 201
    probe_error = resp.json()["probe_error"]
    assert "AccessKeyId" not in probe_error
    assert "LTAI5t-demo-secret-value" not in probe_error
    assert "?" not in probe_error
    # Redis 落库的 probe_error 同样已裁剪（列表接口/前端读取同一来源）
    stored = await fake_redis.hget("aliyndns:accounts:net-fail", "probe_error")
    assert "AccessKeyId" not in stored
    # 列表接口返回值也不含凭证
    list_resp = client.get("/api/aliyun-accounts", headers=auth_headers)
    for acct in list_resp.json():
        assert "AccessKeyId" not in (acct.get("probe_error") or "")
        assert "LTAI5t-demo-secret-value" not in (acct.get("probe_error") or "")


def test_safe_probe_error_strips_query():
    """_safe_probe_error 纯函数：到 "?" 即截断，凭证与签名参数全丢弃。"""
    import api.aliyun_accounts as mod
    url_msg = ("HTTPSConnectionPool(host='alidns.cn-hangzhou.aliyuncs.com', port=443): "
               "GET https://alidns.cn-hangzhou.aliyuncs.com/?AccessKeyId=LTAI-secret&Signature=x")
    out = mod._safe_probe_error("", url_msg)
    assert "AccessKeyId" not in out and "LTAI-secret" not in out and "?" not in out
    # code 优先时同样裁剪（code 本身不含 query，防御式）
    out2 = mod._safe_probe_error("InvalidAccessKeyId.NotFound", url_msg)
    assert out2 == "InvalidAccessKeyId.NotFound"
    # 无 query 的错误消息原样保留（截断到 200 字符）
    long_msg = "x" * 300
    assert len(mod._safe_probe_error("", long_msg)) == 200


def test_probe_real_sdk_with_fake_credentials():
    """真实 SDK 探活路径：假凭证应返回阿里云凭证错误，而非 RuntimeOptions 类型错误。

    回归防线：_probe 曾传 {} 给 describe_domains_with_options 触发
    "'dict' object has no attribute 'key'"（生产实测）。传 RuntimeOptions()
    后假凭证应走到阿里云 API 层（InvalidAccessKeyId），错误码而非 AttributeError。
    """
    import asyncio
    import api.aliyun_accounts as mod
    result = asyncio.run(mod._probe("LTAI-probe-test", "invalid-secret", "cn-hangzhou"))
    assert result["ok"] is False
    # 关键断言：错误是阿里云凭证类错误，不是 SDK 内部类型错误
    assert "attribute 'key'" not in result["error"].lower()
    assert "dict" not in result["error"].lower()


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


async def test_delete_account_resyncs_token_union(no_probe, client, fake_redis, auth_headers, monkeypatch):
    """删除唯一授权账户后，gateway token 的 server 级 union 同步归零（防权限残留）。

    同时断言 publish 了 token:changed——union 归零 = 权限收紧，proxy 缓存
    必须即时失效而非等 60s TTL（Review finding）。
    """
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

    publishes = []
    async def counting_publish(channel, message):
        publishes.append((channel, message))
    monkeypatch.setattr(fake_redis, "publish", counting_publish)
    resp = client.delete("/api/aliyun-accounts/acct1", headers=auth_headers)
    assert resp.status_code == 204
    # 唯一授权被清空 → 授权映射 key 已删
    assert not await fake_redis.exists(f"aliyndns:token_accounts:{tok}")
    # server 级 union 归零，与 GET perms 返回 {} 一致
    perms = json.loads((await fake_redis.hgetall(f"tokens:{token_hash}"))["permissions"])
    assert perms["aliyun-dns-mcp"] == {"read": False, "write": False}
    # 权限收紧必须广播 token:changed（proxy 缓存即时失效）
    token_changed = [m for ch, m in publishes if ch == "token:changed"]
    assert len(token_changed) == 1
    assert json.loads(token_changed[0]) == {"token_hash": token_hash}


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
