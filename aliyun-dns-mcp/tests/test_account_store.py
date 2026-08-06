"""AccountStore 测试：启动加载、热更新、token 权限缓存。"""
import asyncio
import json
import pytest

from account_store import AccountStore, ACCOUNTS_INDEX, CHANGE_CHANNEL


async def _seed_account(r, account_id="acct1", secret="sk-secret"):
    # fakeredis 2.x 的 aioredis.FakeRedis 是纯 async client：不 await 只拿到
    # 未启动的 coroutine，数据写不进去（gateway-admin 测试踩过同样的坑）
    await r.hset(f"aliyndns:accounts:{account_id}", mapping={
        "access_key_id": "LTAI-test",
        "access_key_secret": secret,
        "description": "测试账户",
        "region": "cn-hangzhou",
        "enabled": "true",
        "created_at": "2026-08-06T00:00:00Z",
    })
    await r.sadd(ACCOUNTS_INDEX, account_id)


async def _seed_token_perms(r, token_id="tokid_1", account_id="acct1"):
    await r.hset(f"aliyndns:token_accounts:{token_id}", account_id,
                 json.dumps({"read": True, "write": False}))


@pytest.mark.asyncio
async def test_load_all_reads_accounts_and_index(fake_redis):
    await _seed_account(fake_redis)
    store = AccountStore(fake_redis)
    await store.load_all()
    assert store.account_ids() == {"acct1"}
    creds = store.get_credentials("acct1")
    assert creds["access_key_id"] == "LTAI-test"
    assert creds["enabled"] is True


@pytest.mark.asyncio
async def test_credentials_normalize_enabled(fake_redis):
    await _seed_account(fake_redis)
    await fake_redis.hset("aliyndns:accounts:acct1", "enabled", "false")
    store = AccountStore(fake_redis)
    await store.load_all()
    assert store.get_credentials("acct1")["enabled"] is False


@pytest.mark.asyncio
async def test_get_token_perms_lazy_load(fake_redis):
    await _seed_token_perms(fake_redis)
    store = AccountStore(fake_redis)
    assert store.get_token_perms("tokid_1") == {}  # 未加载 → 空
    await store.ensure_token_loaded("tokid_1")
    assert store.get_token_perms("tokid_1") == {"acct1": {"read": True, "write": False}}
    # 未授权 token → 空 dict
    await store.ensure_token_loaded("tokid_none")
    assert store.get_token_perms("tokid_none") == {}


@pytest.mark.asyncio
async def test_hot_reload_on_channel_message(fake_redis):
    await _seed_account(fake_redis, account_id="acct1")
    store = AccountStore(fake_redis)
    await store.start()
    # 新增账户 + publish → 监听循环 reload
    await _seed_account(fake_redis, account_id="acct2")
    await fake_redis.publish(CHANGE_CHANNEL, json.dumps({"action": "upsert", "key": "aliyndns:accounts:acct2"}))
    # 等待 listener 处理（poll 至多 2s）
    for _ in range(20):
        await asyncio.sleep(0.1)
        if store.account_ids() == {"acct1", "acct2"}:
            break
    assert store.account_ids() == {"acct1", "acct2"}
    await store.close()
