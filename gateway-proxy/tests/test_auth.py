import json
import pytest
from auth import hash_token, verify_token, check_permission


@pytest.fixture(autouse=True)
def clear_token_cache():
    """每个测试前清空模块级 token 缓存，避免测试间串扰。"""
    import auth
    auth.clear_token_cache()


def test_hash_token_is_sha256_hex():
    h = hash_token("tok_abc")
    assert len(h) == 64
    assert h == hash_token("tok_abc")  # deterministic
    assert h != hash_token("tok_xyz")  # different input


async def test_verify_token_valid(fake_redis):
    await fake_redis.hset(
        f"tokens:{hash_token('tok_secret')}",
        mapping={
            "id": "tok_id_1",
            "name": "zabbix-readonly",
            "permissions": '{"zabbix": {"read": true, "write": false}}',
        },
    )
    info = await verify_token("tok_secret")
    assert info is not None
    assert info["name"] == "zabbix-readonly"
    assert info["permissions"]["zabbix"] == {"read": True, "write": False}


async def test_verify_token_invalid_returns_none(fake_redis):
    info = await verify_token("tok_nonexistent")
    assert info is None


def test_check_permission_read_allowed():
    info = {"permissions": {"zabbix": {"read": True, "write": False}}}
    assert check_permission(info, "zabbix", "read") is True


def test_check_permission_write_denied():
    info = {"permissions": {"zabbix": {"read": True, "write": False}}}
    assert check_permission(info, "zabbix", "write") is False


def test_check_permission_server_not_granted():
    info = {"permissions": {"zabbix": {"read": True, "write": False}}}
    assert check_permission(info, "github", "read") is False


# ─── token 本地 TTL 缓存 ───────────────────────────────────────


def _counting_redis(monkeypatch, real_redis):
    """monkeypatch get_redis：返回带 hgetall 调用计数的 wrapper。"""
    import auth
    import redis_client
    calls = {"hgetall": 0}
    real_hgetall = real_redis.hgetall

    class CountingRedis:
        async def hgetall(self, key):
            calls["hgetall"] += 1
            return await real_hgetall(key)

    monkeypatch.setattr(redis_client, "_redis", CountingRedis())
    return calls


async def test_verify_token_cache_hit_no_redis(monkeypatch, fake_redis):
    """缓存命中免 Redis：第二次 verify 不触发 hgetall（LRU + TTL 内）。"""
    import auth
    await fake_redis.hset(
        f"tokens:{hash_token('tok_abc')}",
        mapping={
            "id": "tok_id_1",
            "name": "test",
            "permissions": '{"zabbix": {"read": true, "write": false}}',
        },
    )
    calls = _counting_redis(monkeypatch, fake_redis)

    info = await auth.verify_token("tok_abc")
    assert info is not None
    assert calls["hgetall"] == 1
    # 第二次命中缓存，不再打 Redis
    info2 = await auth.verify_token("tok_abc")
    assert info2 == info
    assert calls["hgetall"] == 1


async def test_verify_token_cache_invalidate(monkeypatch, fake_redis):
    """invalidate_token_cache 后重新走 Redis，读到新权限。"""
    import auth
    await fake_redis.hset(
        f"tokens:{hash_token('tok_abc')}",
        mapping={
            "id": "tok_id_1",
            "name": "test",
            "permissions": '{"zabbix": {"read": true, "write": false}}',
        },
    )
    calls = _counting_redis(monkeypatch, fake_redis)

    await auth.verify_token("tok_abc")
    assert calls["hgetall"] == 1

    # 权限变更 + 缓存失效 → 再查读到新值
    await fake_redis.hset(
        f"tokens:{hash_token('tok_abc')}",
        "permissions",
        json.dumps({"zabbix": {"read": True, "write": True}}),
    )
    auth.invalidate_token_cache(auth.hash_token("tok_abc"))
    info = await auth.verify_token("tok_abc")
    assert info["permissions"]["zabbix"] == {"read": True, "write": True}
    assert calls["hgetall"] == 2


async def test_verify_token_invalid_cached_no_repeated_redis(monkeypatch, fake_redis):
    """invalid token 缓存为 None：第二次验证不重复打 Redis（防空查风暴）。"""
    import auth
    calls = _counting_redis(monkeypatch, fake_redis)

    info = await auth.verify_token("tok_nonexistent")
    assert info is None
    assert calls["hgetall"] == 1
    # 已缓存为 None → 不再打 Redis
    info2 = await auth.verify_token("tok_nonexistent")
    assert info2 is None
    assert calls["hgetall"] == 1
