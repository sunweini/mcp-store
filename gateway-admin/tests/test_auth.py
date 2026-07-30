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
