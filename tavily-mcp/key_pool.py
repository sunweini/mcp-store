"""Single-provider API key pool — Redis-driven rotation + failover.

One copy per MCP (tavily/brave/serpapi); error-kind mapping differs per
provider, rotation logic identical. Design: keys live in Redis
(search:keys:<provider>) so gateway-admin can manage them at runtime and
all MCP instances share the same pool. Pub/Sub channel search:keys:channel
triggers hot reload so admin edits take effect without restart.

OBS: key_id 与 key 明文均不得写入日志/metrics（高基数+敏感）。
"""
import calendar
import json
import time
import uuid
from enum import Enum

import structlog

logger = structlog.get_logger()

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

    async def start(self) -> None:
        await self.reload()
        # Subscribe in a background task; messages trigger reload().
        import asyncio
        asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        while True:
            try:
                msg = await self._pubsub.get_message(ignore_subscribe=True, timeout=30)
                if msg and msg.get("type") == "message":
                    await self.reload()
            except Exception:
                # Redis 短暂故障不致命 — 保留现有池，等待下次通知
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
                               provider=self.provider, error="bad_json")
                continue
            rec["key_id"] = key_id
            records[key_id] = rec
            key_hash[rec["key"]] = key_id
        self._records = records
        self._key_hash = key_hash
        logger.info("key_pool_reloaded",
                    provider=self.provider, key_count=len(records))

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
                if until and _parse_iso(until) > now:
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
        rec["status"] = "active"
        rec["cooldown_until"] = None
        rec["last_used_at"] = _now_iso()
        rec["last_error"] = None
        if remaining is not None:
            rec["remaining"] = remaining
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
