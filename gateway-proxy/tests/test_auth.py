import pytest
from auth import hash_token, verify_token, check_permission


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
