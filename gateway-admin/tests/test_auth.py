"""Tests for admin auth: bcrypt passwords + JWT sessions + /api/login."""
import pytest
from auth import hash_password, verify_password, create_jwt, decode_jwt, mask_token


def test_hash_and_verify_password():
    h = hash_password("secret123")
    assert h != "secret123"
    assert verify_password("secret123", h) is True
    assert verify_password("wrong", h) is False


def test_create_and_decode_jwt():
    tok = create_jwt("admin")
    assert tok.startswith("eyJ")
    sub = decode_jwt(tok)
    assert sub == "admin"


def test_decode_invalid_jwt_returns_none():
    assert decode_jwt("not.a.jwt") is None


def test_mask_token():
    assert mask_token("tok_9f3kq8zabbix001") == "tok_9f3k****"
    assert mask_token("short") == "****"


async def test_login_success(client, fake_redis):
    from auth import hash_password, ensure_default_admin
    await fake_redis.hset("admin:admin", mapping={
        "password_hash": hash_password("admin123"),
        "role": "admin",
    })
    resp = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data
    assert data["expires_in"] == 86400


def test_login_wrong_password(client, fake_redis):
    resp = client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


async def test_ensure_default_admin_uses_env_password(monkeypatch, fake_redis):
    """ADMIN_INIT_PASSWORD 设了 -> 用该密码建 admin。"""
    monkeypatch.setenv("ADMIN_INIT_PASSWORD", "strong-pass-456")
    from auth import ensure_default_admin, verify_password
    await ensure_default_admin()
    stored = await fake_redis.hgetall("admin:admin")
    assert verify_password("strong-pass-456", stored["password_hash"]) is True
    assert verify_password("admin123", stored["password_hash"]) is False


async def test_ensure_default_admin_falls_back_to_default(monkeypatch, fake_redis):
    """没设 ADMIN_INIT_PASSWORD -> 回退 admin123。"""
    monkeypatch.delenv("ADMIN_INIT_PASSWORD", raising=False)
    from auth import ensure_default_admin, verify_password
    await ensure_default_admin()
    stored = await fake_redis.hgetall("admin:admin")
    assert verify_password("admin123", stored["password_hash"]) is True


async def test_ensure_default_admin_idempotent(monkeypatch, fake_redis):
    """admin 已存在 -> 不覆盖。"""
    monkeypatch.setenv("ADMIN_INIT_PASSWORD", "new-pass")
    from auth import ensure_default_admin, hash_password, verify_password
    await fake_redis.hset("admin:admin", mapping={"password_hash": hash_password("existing")})
    await ensure_default_admin()
    stored = await fake_redis.hgetall("admin:admin")
    assert verify_password("existing", stored["password_hash"]) is True
    assert verify_password("new-pass", stored["password_hash"]) is False
