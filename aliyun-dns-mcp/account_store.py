"""Redis 账户凭证 + token 账户权限存储，Pub/Sub 热更新。

MCP 是账户级权限的权威，这里持有全部账户凭证与 token→账户权限映射的
内存缓存；gateway-admin 写入 Redis 后 PUBLISH aliyndns:changed，本类
监听并全量重载（小规模，全量加载成本可忽略；热更新免重启）。

安全：AccessKey/Secret 明文只存在于 Redis 值与内存，禁入日志/metric。
"""
import asyncio
import json

import structlog

logger = structlog.get_logger()

ACCOUNTS_INDEX = "aliyndns:accounts:index"
CHANGE_CHANNEL = "aliyndns:changed"


class AccountStore:
    def __init__(self, redis):
        self._redis = redis
        self._accounts: dict[str, dict] = {}
        self._token_perms_cache: dict[str, dict[str, dict]] = {}
        self._listener_task: asyncio.Task | None = None
        self._listening = False
        self._subscribe_done: asyncio.Event | None = None

    async def start(self) -> None:
        """加载全量 + 启动热更新监听。listener 必须与 server 同 event loop。

        NOTE: 等 subscribe 确认后才返回——否则调用方紧接着 PUBLISH 的消息
        会被未就绪的订阅丢失（fakeredis 2.37 实测；真实 Redis 同样存在
        subscribe→publish 竞态窗口，publisher 无法感知订阅者是否就绪）。
        """
        await self.load_all()
        self._subscribe_done = asyncio.Event()
        self._listening = True
        self._listener_task = asyncio.create_task(self._listen())
        try:
            await asyncio.wait_for(self._subscribe_done.wait(), timeout=30)
        except asyncio.TimeoutError:
            # 订阅未确认说明 listener 已异常退出（如连接拒绝），
            # 立即失败让调用方重试/告警，而不是带着死 listener 继续跑
            self._listening = False
            self._listener_task.cancel()
            raise RuntimeError("account_store pubsub 订阅超时（listener 启动失败）") from None

    async def close(self) -> None:
        self._listening = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass

    async def load_all(self) -> None:
        r = self._redis
        accounts = {}
        for account_id in await r.smembers(ACCOUNTS_INDEX):
            data = await r.hgetall(f"aliyndns:accounts:{account_id}")
            if not data:
                continue
            accounts[account_id] = self._normalize_creds(data)
        self._accounts = accounts
        # 权限映射懒加载缓存：账户变更会连带清缓存（权限值依赖账户存在性）
        self._token_perms_cache.clear()
        logger.info("account_store_loaded", service="aliyun-dns-mcp", accounts=len(accounts))

    async def disable_account(self, account_id: str) -> None:
        """凭证失效时禁用账户（I3，spec §7.1 闭环）。

        为什么：工具层实测发现 INVALID_CREDENTIAL 时说明该账户凭证已被
        阿里云吊销/轮换——继续放行只会让每次调用都带错凭证，且写操作
        （add/update/delete_record）会带着失效凭证失败但用户无法察觉原因。
        幂等：已禁用再次调用无害；PUBLISH 触发热更新让本进程与其他副本
        同步感知。凭证安全：日志只记 account_id，不记任何凭证内容。
        """
        await self._redis.hset(f"aliyndns:accounts:{account_id}", "enabled", "false")
        # listener 重载后 _accounts 内存缓存同步生效（异步，本方法返回时
        # 工具层已 fail-closed 返回错误，不依赖缓存立即更新）
        await self._redis.publish(CHANGE_CHANNEL, json.dumps(
            {"action": "disable", "key": f"aliyndns:accounts:{account_id}"}))
        logger.error("aliyun_account_auto_disabled", service="aliyun-dns-mcp",
                     account_id=account_id,
                     reason="invalid_credential detected by API call (spec §7.1)")

    @staticmethod
    def _normalize_creds(data: dict) -> dict:
        return {
            "access_key_id": data.get("access_key_id", ""),
            "access_key_secret": data.get("access_key_secret", ""),
            "description": data.get("description", ""),
            "region": data.get("region", "cn-hangzhou"),
            "enabled": data.get("enabled", "true") == "true",
        }

    # ── 同步读（内存缓存）────────────────────────────────────────
    def get_credentials(self, account_id: str) -> dict | None:
        return self._accounts.get(account_id)

    def account_exists(self, account_id: str) -> bool:
        return account_id in self._accounts

    def account_ids(self) -> set[str]:
        return set(self._accounts)

    def get_token_perms(self, token_id: str) -> dict[str, dict]:
        """token 的账户级权限 {account_id: {"read", "write"}}；未加载返回 {}。

        只读缓存不插入——未加载的 key 保持缺失，ensure_token_loaded 据此
        判断需要真正加载（若在此插入空 dict，加载入口会误判已加载而跳过）。
        """
        return self._token_perms_cache.get(token_id, {})

    async def load_token_perms(self, token_id: str) -> dict[str, dict]:
        """从 Redis 加载某个 token 的权限（缓存未命中时调用）。"""
        raw = await self._redis.hgetall(f"aliyndns:token_accounts:{token_id}")
        perms = {}
        for account_id, payload in raw.items():
            try:
                p = json.loads(payload)
                # 必须 `is True` 而不是 bool()：JSON 反序列化后真值是布尔 True，
                # 但 Redis 可被手写 "false" 字符串（gateway-admin 之外的写入者），
                # bool("false")==True 会把拒绝权限反转成放行（M1 审查发现）
                perms[account_id] = {"read": p.get("read") is True, "write": p.get("write") is True}
            except json.JSONDecodeError:
                logger.warning("token_perms_corrupt", service="aliyun-dns-mcp",
                               token_id=token_id, account_id=account_id)
        self._token_perms_cache[token_id] = perms
        return perms

    async def ensure_token_loaded(self, token_id: str) -> None:
        """确保某 token 的权限已加载（懒加载入口，auth 校验前调用）。"""
        if token_id not in self._token_perms_cache:
            await self.load_token_perms(token_id)

    # ── 热更新监听 ────────────────────────────────────────────────
    async def _listen(self) -> None:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(CHANGE_CHANNEL)
        # 通知 start() 订阅已就绪（对应 start() 里的等待）
        if self._subscribe_done:
            self._subscribe_done.set()
        while self._listening:
            try:
                msg = await pubsub.get_message(timeout=30)
                if msg and msg.get("type") == "message":
                    await self.load_all()
            except Exception:
                # redis-py 连接死后不自动重连：必须重建 pubsub 订阅，
                # 否则热更新永久失效只能重启进程（serpapi 踩坑教训）
                try:
                    await pubsub.aclose()
                except Exception:
                    pass
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(CHANGE_CHANNEL)
                await asyncio.sleep(5)
