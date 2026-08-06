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
async def test_load_token_perms_string_false_not_truthy(fake_redis):
    """M1 回归：Redis 手写字符串 "false" 不得被 bool() 反转为 True（权限反转）。"""
    # 模拟非 gateway-admin 写入者：payload 里 read/write 是字符串而非布尔
    await fake_redis.hset("aliyndns:token_accounts:tokid_1", "acct1",
                          json.dumps({"read": "false", "write": "false"}))
    store = AccountStore(fake_redis)
    await store.ensure_token_loaded("tokid_1")
    perms = store.get_token_perms("tokid_1")["acct1"]
    assert perms["read"] is False
    assert perms["write"] is False


@pytest.mark.asyncio
async def test_disable_account_marks_disabled_and_publishes(fake_redis):
    """I3 回归：disable_account 写 enabled=false + PUBLISH 热更新 + 内存缓存生效。"""
    await _seed_account(fake_redis)
    store = AccountStore(fake_redis)
    await store.start()
    assert store.get_credentials("acct1")["enabled"] is True

    await store.disable_account("acct1")
    # Redis 落盘
    assert await fake_redis.hget("aliyndns:accounts:acct1", "enabled") == "false"
    # 热更新通知已发布（listener 重载后内存缓存同步）
    for _ in range(20):
        await asyncio.sleep(0.1)
        if store.get_credentials("acct1")["enabled"] is False:
            break
    assert store.get_credentials("acct1")["enabled"] is False
    # 幂等：重复禁用无害
    await store.disable_account("acct1")
    assert await fake_redis.hget("aliyndns:accounts:acct1", "enabled") == "false"
    await store.close()


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
