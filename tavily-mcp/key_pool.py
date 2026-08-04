"""Single-provider API key pool — Redis-driven rotation + failover.

One copy per MCP (tavily/brave/serpapi); error-kind mapping differs per
provider, rotation logic identical. Design: keys live in Redis
(search:keys:<provider>) so gateway-admin can manage them at runtime and
all MCP instances share the same pool. Pub/Sub channel search:keys:channel
triggers hot reload so admin edits take effect without restart.

OBS: key_id 与 key 明文均不得写入日志/metrics（高基数+敏感）。
"""
import asyncio
import calendar
import json
import time
import uuid
from enum import Enum

import structlog

logger = structlog.get_logger()

try:
    from telemetry import record_quota_metrics
except ImportError:
    # telemetry 依赖缺失（精简部署/测试环境）时指标写入口降级 no-op——
    # 与 tools/__init__.py 的防御式导入同一模式，池功能不依赖指标
    def record_quota_metrics(provider: str, snapshot: dict) -> None:
        return None

LOW_QUOTA_RATIO = 0.05      # remaining/quota < 5% → skip, fallback only
WARN_QUOTA_RATIO = 0.10     # remaining/quota < 10% → warning, still used
DEFAULT_COOLDOWN_SECONDS = 30
RETRY_AFTER_LIMIT = 600     # cap Retry-After to avoid absurd cooldowns


class ErrorKind(str, Enum):
    INVALID = "invalid"          # 401/403 — key 失效，永久剔除
    EXHAUSTED = "exhausted"      # 配额耗尽/欠费 — 永久剔除
    RATE_LIMIT = "rate_limit"    # 429 — 冷却后恢复


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _key_id(provider: str) -> str:
    """URL-safe opaque id, not derived from the key itself."""
    return f"{provider}_{uuid.uuid4().hex[:12]}"


class KeyPool:
    """Redis-backed key pool for one provider.

    redis: async redis client (decode_responses=True).
    pubsub: async PubSub object on channel search:keys:channel.
    quota_default: monthly quota used when a key lacks monthly_quota.
    """

    def __init__(self, provider: str, redis, pubsub, quota_default: int):
        self.provider = provider
        self._redis = redis
        self._pubsub = pubsub
        self._quota_default = quota_default
        self._records: dict[str, dict] = {}
        self._key_hash: dict[str, str] = {}  # key → key_id (decorrelation)
        self._pool_key = f"search:keys:{provider}"
        # 持有监听任务引用防 GC（3.12 对无引用任务有销毁告警）；
        # done_callback 记录异常退出，重启策略由 Task 2 server 层决定
        self._listen_task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.reload()
        # Subscribe in a background task; messages trigger reload().
        self._listen_task = asyncio.create_task(self._listen())
        self._listen_task.add_done_callback(self._on_listen_exit)

    def _on_listen_exit(self, task: asyncio.Task) -> None:
        # 监听任务异常退出时记录（不重启，保留现场供 server 层决策；
        # _listen 自身已捕获单次故障，走到这里说明 pubsub 长期不可用）
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("key_pool_listener_stopped",
                             service="tavily-mcp",
                             provider=self.provider,
                             error=type(exc).__name__)

    async def _listen(self) -> None:
        while True:
            try:
                # 不传 ignore_subscribe kwarg：redis-py 6+ 已改名
                # ignore_subscribe_messages（旧名直接 TypeError——本机
                # 8.1.0 实测），改名前的旧版本才认旧名。省略该参数则
                # 各版本通用；subscribe 确认消息（type="subscribe"）由
                # 下方 type=="message" 过滤天然排除，无需显式忽略。
                msg = await self._pubsub.get_message(timeout=30)
                if msg and msg.get("type") == "message":
                    await self.reload()
            except Exception:
                # Redis 短暂故障不致命 — 保留现有池，等待下次通知；
                # 有日志便于排障（热更新静默失效是最难查的问题之一）
                logger.warning("key_pool_listen_retry",
                               service="tavily-mcp",
                               provider=self.provider, error="pubsub_error")
                await asyncio.sleep(5)

    async def reload(self) -> None:
        raw = await self._redis.hgetall(self._pool_key)
        records: dict[str, dict] = {}
        key_hash: dict[str, str] = {}
        for key_id, payload in raw.items():
            try:
                rec = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("key_pool_skip_bad_record",
                               service="tavily-mcp",
                               provider=self.provider, error="bad_json")
                continue
            rec["key_id"] = key_id
            records[key_id] = rec
            key_hash[rec["key"]] = key_id
        self._records = records
        self._key_hash = key_hash
        logger.info("key_pool_reloaded",
                    service="tavily-mcp",
                    provider=self.provider, key_count=len(records))
        # 池状态刚整体刷新（热更新/启动）——配额告警指标同步刷新，
        # 否则 admin 侧改配额/增删 key 后 scrape 数据停留在旧值
        record_quota_metrics(self.provider, self.health_snapshot())

    async def next_key(self) -> dict | None:
        """Pick the best key. Priority:
        1. enabled, status != invalid/exhausted, cooldown expired
        2. skip low_quota unless no healthy key remains (fallback)
        3. highest remaining (quota-aware), tie by insertion order
        Returns the full record dict, or None if pool empty.
        """
        now = time.time()
        healthy, low_quota, unavailable = [], [], []
        for rec in self._records.values():
            if not rec.get("enabled", True) or rec.get("status") in ("invalid", "exhausted"):
                unavailable.append(rec)
                continue
            if rec.get("status") == "cooldown":
                until = rec.get("cooldown_until")
                if until:
                    try:
                        cooldown_active = _parse_iso(until) > now
                    except (ValueError, TypeError):
                        # 畸形 cooldown_until（管理端脏数据）：按已过期处理，
                        # 避免脏数据导致 key 永久不可用，也不向工具层冒泡
                        cooldown_active = False
                    if cooldown_active:
                        unavailable.append(rec)
                        continue
                rec["status"] = "active"
                rec["cooldown_until"] = None
            ratio = self._ratio(rec)
            if ratio is not None and ratio < LOW_QUOTA_RATIO:
                rec["status"] = "low_quota"
                low_quota.append(rec)
            else:
                if ratio is not None and ratio < WARN_QUOTA_RATIO:
                    rec["status"] = "low_quota_warning"
                healthy.append(rec)

        candidates = healthy if healthy else low_quota
        if not candidates:
            return None
        candidates.sort(key=lambda r: r.get("remaining") or 0, reverse=True)
        return candidates[0]

    async def on_success(self, key_id: str, remaining: int | None = None) -> None:
        rec = self._records.get(key_id)
        if rec is None:
            return
        rec["cooldown_until"] = None
        rec["last_used_at"] = _now_iso()
        rec["last_error"] = None
        if remaining is not None:
            rec["remaining"] = remaining
        # 成功不清低配额状态：按最新 remaining 重算档位并持久化——前台
        # API Keys 页读 Redis status 展示低配额告警，原实现无条件置
        # active 会覆盖 next_key 算出的 low_quota 状态，告警永远看不到
        ratio = self._ratio(rec)
        if ratio is None:
            rec["status"] = "active"
        elif ratio < LOW_QUOTA_RATIO:
            rec["status"] = "low_quota"
        elif ratio < WARN_QUOTA_RATIO:
            rec["status"] = "low_quota_warning"
        else:
            rec["status"] = "active"
        await self._write(key_id, rec)
        # 本地用量计数：ZSet member=now, score=now（按月窗口统计）
        now = time.time()
        await self._redis.zadd(f"search:usage:{self.provider}:{key_id}", {str(now): now})
        await self._redis.expire(f"search:usage:{self.provider}:{key_id}", 60 * 24 * 32)

    async def on_error(self, key_id: str, kind: ErrorKind,
                       retry_after: int | None = None) -> None:
        rec = self._records.get(key_id)
        if rec is None:
            return
        if kind == ErrorKind.INVALID:
            rec["status"] = "invalid"
            rec["cooldown_until"] = None
            # key 被剔除是池健康状态的关键变化——立即刷新告警指标，
            # 否则 invalid_count 增长要到下次 reload 才反映到 Prometheus
            # （EXHAUSTED 同样永久剔除，但 remaining=0 已由 quota 指标
            # 覆盖；RATE_LIMIT 是临时冷却不改变池长期健康，不刷新）
            record_quota_metrics(self.provider, self.health_snapshot())
        elif kind == ErrorKind.EXHAUSTED:
            rec["status"] = "exhausted"
            rec["cooldown_until"] = None
            rec["remaining"] = 0
        elif kind == ErrorKind.RATE_LIMIT:
            rec["status"] = "cooldown"
            seconds = min(retry_after or DEFAULT_COOLDOWN_SECONDS, RETRY_AFTER_LIMIT)
            rec["cooldown_until"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))
        rec["last_error"] = kind.value
        await self._write(key_id, rec)

    def health_snapshot(self) -> dict:
        """池健康摘要（spec KeyPool 设计节）——配额指标的数据源。

        返回 {lowest_ratio, lowest_remaining, pool_size, invalid_count}：
        - lowest_ratio / lowest_remaining：该源所有 key 中剩余配额最低者
          （spec 可观测性节：指标按 provider 聚合，取最低档告警，避免
          key 级高基数 label）。remaining 未知的 key 不参与 ratio 计算
          （无官方数据无法算占比），但 remaining=0 的已耗尽 key 永远
          是最低档（其 ratio 为 0）。
        - pool_size：池内 key 总数（含 invalid/exhausted——大小反映
          管理面配置规模，不是可用数）
        - invalid_count：invalid 状态 key 数（告警「key 大量失效」用）
        纯内存计算（不触 Redis）——reload/on_error 后同步调用无 IO 开销。
        """
        ratios: list[float] = []
        remaining_vals: list[int] = []
        pool_size = 0
        invalid_count = 0
        for rec in self._records.values():
            pool_size += 1
            if rec.get("status") == "invalid":
                invalid_count += 1
            remaining = rec.get("remaining")
            if isinstance(remaining, (int, float)):
                remaining_vals.append(remaining)
            ratio = self._ratio(rec)
            if ratio is not None:
                ratios.append(ratio)
        return {
            "lowest_ratio": min(ratios) if ratios else None,
            # 无任何 remaining 数据时为 None（unknown-quota 池不应误报
            # 耗尽）；remaining=0 的 key 会给出 0，让 exhausted 档可触发
            "lowest_remaining": min(remaining_vals) if remaining_vals else None,
            "pool_size": pool_size,
            "invalid_count": invalid_count,
        }

    async def probe(self, key: str) -> dict:
        """Probe a key at add-time. Returns record dict with status
        active/invalid + remaining. Subclasses override to call provider API."""
        raise NotImplementedError

    def _ratio(self, rec: dict) -> float | None:
        quota = rec.get("monthly_quota") or self._quota_default
        remaining = rec.get("remaining")
        if remaining is None or quota <= 0:
            return None
        return remaining / quota

    async def _write(self, key_id: str, rec: dict) -> None:
        await self._redis.hset(self._pool_key, key_id, json.dumps(rec, ensure_ascii=False))


def _parse_iso(iso: str) -> float:
    """Parse our own %Y-%m-%dT%H:%M:%SZ format (no external dep).

    cooldown_until 由 gmtime 生成（UTC）；time.mktime 按本地时区解析会
    引入时区偏移，导致冷却期被提前判为过期，故用 calendar.timegm。
    """
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
