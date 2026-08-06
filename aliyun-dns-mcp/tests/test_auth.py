"""PermissionChecker 测试：token 验证 + 账户级 read/write 判定。"""
import hashlib
import json

import pytest
from fastmcp.exceptions import ToolError

from account_store import AccountStore, ACCOUNTS_INDEX
from auth import PermissionChecker, hash_token, extract_token


async def _seed_token(r, token="tok_abc", token_id="tokid_1"):
    # fakeredis 2.x aioredis.FakeRedis 是纯 async client：不 await 只拿到
    # 未启动的 coroutine，数据写不进去（test_account_store.py 同坑）
    await r.hset(f"tokens:{hash_token(token)}", mapping={
        "id": token_id, "name": "test-token", "permissions": "{}",
    })


async def _seed_account(r, account_id="acct1"):
    await r.hset(f"aliyndns:accounts:{account_id}", mapping={
        "access_key_id": "LTAI-test", "access_key_secret": "sk", "description": "账户1",
        "region": "cn-hangzhou", "enabled": "true",
    })
    await r.sadd(ACCOUNTS_INDEX, account_id)


async def _seed_token_perms(r, token_id, mapping):
    await r.hset(f"aliyndns:token_accounts:{token_id}",
                 mapping={a: json.dumps(p) for a, p in mapping.items()})


def test_hash_token_sha256():
    h = hash_token("tok_abc")
    assert len(h) == 64
    assert h == hashlib.sha256(b"tok_abc").hexdigest()


def test_extract_token():
    assert extract_token({"authorization": "Bearer abc123"}) == "abc123"
    assert extract_token({"authorization": "bearer abc123"}) == "abc123"
    assert extract_token({}) is None
    assert extract_token(None) is None


def make_checker(redis):
    store = AccountStore(redis)
    return PermissionChecker(store, redis), store


@pytest.mark.asyncio
async def test_require_missing_header_denied(fake_redis, monkeypatch):
    checker, _ = make_checker(fake_redis)
    monkeypatch.setattr("auth.get_http_headers", lambda include_all=False: {})
    with pytest.raises(ToolError) as e:
        await checker.require("acct1", "read")
    assert "invalid_token" in str(e.value)


@pytest.mark.asyncio
async def test_require_invalid_token_denied(fake_redis, monkeypatch):
    checker, _ = make_checker(fake_redis)
    monkeypatch.setattr("auth.get_http_headers",
                        lambda include_all=False: {"authorization": "Bearer bad-token"})
    with pytest.raises(ToolError) as e:
        await checker.require("acct1", "read")
    assert "invalid_token" in str(e.value)


@pytest.mark.asyncio
async def test_require_read_allowed_write_denied(fake_redis, monkeypatch):
    await _seed_token(fake_redis)
    await _seed_account(fake_redis)
    await _seed_token_perms(fake_redis, "tokid_1", {"acct1": {"read": True, "write": False}})
    checker, store = make_checker(fake_redis)
    await store.load_all()
    monkeypatch.setattr("auth.get_http_headers",
                        lambda include_all=False: {"authorization": "Bearer tok_abc"})
    await checker.require("acct1", "read")  # 不抛
    with pytest.raises(ToolError) as e:
        await checker.require("acct1", "write")
    assert "no_permission" in str(e.value)


@pytest.mark.asyncio
async def test_require_unlisted_account_denied(fake_redis, monkeypatch):
    await _seed_token(fake_redis)
    await _seed_account(fake_redis)
    await _seed_token_perms(fake_redis, "tokid_1", {"acct2": {"read": True, "write": False}})
    checker, store = make_checker(fake_redis)
    await store.load_all()
    monkeypatch.setattr("auth.get_http_headers",
                        lambda include_all=False: {"authorization": "Bearer tok_abc"})
    with pytest.raises(ToolError) as e:
        await checker.require("acct1", "read")
    assert "no_permission" in str(e.value)


@pytest.mark.asyncio
async def test_allowed_accounts_filters_disabled(fake_redis, monkeypatch):
    await _seed_token(fake_redis)
    await _seed_account(fake_redis, "acct1")
    await _seed_account(fake_redis, "acct2")
    await fake_redis.hset("aliyndns:accounts:acct2", "enabled", "false")
    await _seed_token_perms(fake_redis, "tokid_1", {
        "acct1": {"read": True, "write": True},
        "acct2": {"read": True, "write": False},
    })
    checker, store = make_checker(fake_redis)
    await store.load_all()
    monkeypatch.setattr("auth.get_http_headers",
                        lambda include_all=False: {"authorization": "Bearer tok_abc"})
    result = await checker.allowed_accounts()
    assert [a["account_id"] for a in result] == ["acct1"]
    assert result[0]["read"] is True and result[0]["write"] is True
