"""Search MCP API key management API.

Owns the search:keys:<provider> Redis Hash that the tavily/brave/serpapi
MCPs read as their key pools. Write → PUBLISH search:keys:channel so
running MCPs hot-reload without restart.

Security: require_admin on every route; keys are stored in Redis plaintext
(inner network, consistent with gateway's token storage). Plaintext key is
returned ONLY in the POST create response; list/detail return key_masked.
Never log key plaintext.
"""
import calendar
import json
import os
import time
import uuid

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import require_admin
from calibrate import calibrate_provider
from redis_client import get_redis

logger = structlog.get_logger()

router = APIRouter(prefix="/api/search-keys", tags=["search-keys"])

PROVIDERS = ("tavily", "brave", "serpapi")
QUOTA_DEFAULTS = {"tavily": 1000, "brave": 2000, "serpapi": 100}


class KeyCreate(BaseModel):
    key: str
    # ge=1：0 会触发 MCP 侧 "quota or default" 回退（key_pool.py），
    # 存 0 会造成管理界面显示与 MCP 实际行为分裂；负数更会污染 usage ratio。
    monthly_quota: int | None = Field(default=None, ge=1)


class KeyUpdate(BaseModel):
    enabled: bool | None = None
    monthly_quota: int | None = Field(default=None, ge=1)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=422, detail=f"provider 必须是 {'/'.join(PROVIDERS)}")
    return provider


async def _publish(action: str, provider: str, key_id: str) -> None:
    """PUBLISH a change notification so MCPs hot-reload their key pool.

    MCPs subscribe to search:keys:channel (keyspace semantics: full reload
    on any action) — the key_id is included for debuggability only.

    Redis 主操作（hset/hdel）已成功即操作成功；publish 失败只记 warning
    不阻断。MCP 侧只有 pubsub 订阅、无轮询兜底，丢消息只能等下次变更
    或重启补——但绝不能让管理面操作 500，否则重试会重复建 key。
    """
    try:
        r = get_redis()
        await r.publish("search:keys:channel",
                        json.dumps({"provider": provider, "action": action, "key_id": key_id}))
    except Exception as e:
        logger.warning("search_key_publish_failed", provider=provider, key_id=key_id,
                       error=str(e), service="gateway-admin")


@router.get("/{provider}")
async def list_keys(provider: str, _: str = Depends(require_admin)):
    _validate_provider(provider)
    r = get_redis()
    out = []
    # hgetall 返回普通 dict（非 pipeline），items() 是普通迭代
    for key_id, payload in (await r.hgetall(f"search:keys:{provider}")).items():
        try:
            rec = json.loads(payload)
        except json.JSONDecodeError:
            # 脏数据不暴露：跳过损坏记录，避免管理界面 500
            logger.warning("search_key_corrupt", provider=provider, key_id=key_id,
                           service="gateway-admin")
            continue
        rec["key_id"] = key_id
        rec["key_masked"] = _mask(rec.get("key", ""))
        rec.pop("key", None)
        rec["month_usage"] = await _month_usage(provider, key_id)
        out.append(rec)
    return out


@router.post("/calibrate")
async def calibrate_keys(_: str = Depends(require_admin)):
    """官方用量校准：逐源拉官方 usage 接口同步 monthly_quota + remaining。

    必须注册在 POST /{provider} 之前——FastAPI 按注册顺序匹配，否则
    "calibrate" 会被当成 provider 路径参数落入 add_key（422）。
    """
    # brave 无公开用量接口，calibrate_provider 内部返回 supported=false 且不碰记录
    return [await calibrate_provider(p) for p in PROVIDERS]


@router.post("/{provider}", status_code=201)
async def add_key(provider: str, req: KeyCreate, _: str = Depends(require_admin)):
    _validate_provider(provider)
    if not req.key.strip():
        raise HTTPException(status_code=422, detail="key 不能为空")
    r = get_redis()
    key_id = f"{provider}_{uuid.uuid4().hex[:12]}"
    rec = {
        "key": req.key.strip(),
        "provider": provider,
        "enabled": True,
        "monthly_quota": req.monthly_quota or QUOTA_DEFAULTS[provider],
        "status": "active",           # 探活结果会更新
        "cooldown_until": None,
        "remaining": None,
        "last_used_at": None,
        "last_error": None,
        "created_at": _now_iso(),
    }
    # 自动探活：最小查询验证 key 有效性（消耗 1 次配额，见 spec）。
    # 失败不阻断添加——管理员可能要在探活恢复前先入库。
    probe_result = await _probe_key(provider, req.key.strip())
    if not probe_result["ok"]:
        rec["status"] = "invalid"
        rec["last_error"] = probe_result["error"]
    else:
        rec["remaining"] = probe_result.get("remaining")
    await r.hset(f"search:keys:{provider}", key_id, json.dumps(rec, ensure_ascii=False))
    await _publish("upsert", provider, key_id)
    rec["key_id"] = key_id
    logger.info("search_key_added", provider=provider, key_id=key_id,
                status=rec["status"], service="gateway-admin")
    return rec


@router.put("/{provider}/{key_id}")
async def update_key(provider: str, key_id: str, req: KeyUpdate,
                     _: str = Depends(require_admin)):
    _validate_provider(provider)
    r = get_redis()
    payload = await r.hget(f"search:keys:{provider}", key_id)
    if not payload:
        raise HTTPException(status_code=404, detail="key not found")
    try:
        rec = json.loads(payload)
    except json.JSONDecodeError:
        # 与 list_keys 的脏数据策略一致：损坏记录当不存在处理，不 500
        raise HTTPException(status_code=404, detail="key not found")
    if req.enabled is not None:
        rec["enabled"] = req.enabled
    if req.monthly_quota is not None:
        rec["monthly_quota"] = req.monthly_quota
    await r.hset(f"search:keys:{provider}", key_id, json.dumps(rec, ensure_ascii=False))
    await _publish("upsert", provider, key_id)
    logger.info("search_key_updated", provider=provider, key_id=key_id,
                service="gateway-admin")
    return {"key_id": key_id, "enabled": rec["enabled"], "monthly_quota": rec["monthly_quota"]}


@router.delete("/{provider}/{key_id}", status_code=204)
async def delete_key(provider: str, key_id: str, _: str = Depends(require_admin)):
    _validate_provider(provider)
    r = get_redis()
    removed = await r.hdel(f"search:keys:{provider}", key_id)
    if not removed:
        raise HTTPException(status_code=404, detail="key not found")
    await r.delete(f"search:usage:{provider}:{key_id}")
    await _publish("delete", provider, key_id)
    logger.info("search_key_deleted", provider=provider, key_id=key_id,
                service="gateway-admin")
    return None


@router.get("/{provider}/usage")
async def usage(provider: str, _: str = Depends(require_admin)):
    """用量看板：每 key 本地当月计数 + 配额上限 + 剩余（官方 remaining 或估算）。"""
    _validate_provider(provider)
    r = get_redis()
    out = []
    for key_id, payload in (await r.hgetall(f"search:keys:{provider}")).items():
        try:
            rec = json.loads(payload)
        except json.JSONDecodeError:
            continue
        used = await _month_usage(provider, key_id)
        quota = rec.get("monthly_quota") or QUOTA_DEFAULTS[provider]
        remaining = rec.get("remaining")
        if remaining is None:
            # 探活时未取到官方 remaining：本地计数兜底估算
            remaining = max(quota - used, 0)
        out.append({
            "key_id": key_id,
            "key_masked": _mask(rec.get("key", "")),
            "status": rec.get("status"),
            "month_quota": quota,
            "month_usage": used,
            "remaining": remaining,
            "ratio": round(remaining / quota, 4) if quota else None,
        })
    return {"provider": provider, "keys": out}


async def _month_usage(provider: str, key_id: str) -> int:
    """本地计数：ZSet member=时间戳，按月窗口统计当月条数。

    探活不写本地计数（spec：探活消耗官方配额但不计入本地用量），
    因此这里统计的就是真实的搜索请求次数。
    """
    r = get_redis()
    now = time.time()
    month_start = time.strftime("%Y-%m-01T00:00:00Z", time.gmtime(now))
    # gmtime 生成的是 UTC 时间串，必须用 timegm 而不是 mktime 解释，
    # 否则本地时区偏移会把窗口起点算偏（mktime 按本地时区解释）。
    month_start_ts = calendar.timegm(time.strptime(month_start, "%Y-%m-%dT%H:%M:%SZ"))
    members = await r.zrangebyscore(f"search:usage:{provider}:{key_id}",
                                    min=month_start_ts, max="+inf")
    return len(members)


def _mask(key: str) -> str:
    """Key 打码：保留前 4 后 4，中间省略。明文只在添加时返回一次。"""
    if len(key) <= 12:
        return key[:4] + "…"
    return f"{key[:4]}…{key[-4:]}"


async def _probe_key(provider: str, key: str, client: httpx.AsyncClient | None = None) -> dict:
    """探活：发一次最小查询验证 key 有效性（消耗 1 次配额）。

    探活结果计入该 key 配额（官方计数）但不计入本地用量统计（spec 错误处理节）。
    失败返回 {"ok": False, "error": 原因}；成功 {"ok": True, "remaining": int|None}。

    client 参数仅供测试注入 MockTransport——生产调用方不传。
    """
    if client is None:
        # 生产网络 api.search.brave.com 直连不通（IPv4 被墙/IPv6 不通），
        # brave 探活必须与 brave-mcp 走同一内网代理（tavily/serpapi 直连
        # 通，不受影响）；None/空串时 httpx 不启用代理。admin 容器部署时
        # 也需配 SEARCH_PROXY。
        proxy = os.environ.get("SEARCH_PROXY") or None
        client = httpx.AsyncClient(timeout=5, proxy=proxy)
        owns_client = True
    else:
        owns_client = False
    try:
        if provider == "tavily":
            resp = await client.post("https://api.tavily.com/search",
                                     json={"query": "ping", "max_results": 1},
                                     headers={"Authorization": f"Bearer {key}"})
            if resp.status_code != 200:
                return {"ok": False, "error": f"tavily probe HTTP {resp.status_code}: {resp.text[:120]}"}
            remaining = None
            try:
                usage = await client.get("https://api.tavily.com/usage",
                                         headers={"Authorization": f"Bearer {key}"})
                if usage.status_code == 200 and usage.content:
                    body = usage.json()
                    cur = body.get("monthly_usage", {}).get("current_usage")
                    mx = body.get("monthly_usage", {}).get("max_usage")
                    if isinstance(cur, int) and isinstance(mx, int) and mx:
                        # 顺带写 remaining，管理界面免猜官方余量
                        remaining = max(mx - cur, 0)
            except Exception:
                # 查询成功但 usage 拉取失败：不算探活失败，仅剩 remaining=None
                pass
            return {"ok": True, "remaining": remaining}
        if provider == "brave":
            resp = await client.get("https://api.search.brave.com/res/v1/web/search",
                                    params={"q": "ping", "count": 1},
                                    headers={"X-Subscription-Token": key})
            if resp.status_code == 200:
                return {"ok": True, "remaining": None}
            return {"ok": False, "error": f"brave probe HTTP {resp.status_code}: {resp.text[:120]}"}
        if provider == "serpapi":
            resp = await client.get("https://serpapi.com/search.json",
                                    params={"engine": "google", "q": "ping",
                                            "api_key": key, "num": 1})
            # serpapi 的"成功"是把错误放进 200 响应体（如 quota 超限），
            # 且错误响应也可能没有 body——resp.content 为空时不能调 json()。
            body = resp.json() if resp.content else {}
            if resp.status_code == 200 and "error" not in body:
                return {"ok": True, "remaining": None}
            return {"ok": False, "error": f"serpapi probe HTTP {resp.status_code}: {resp.text[:120]}"}
    except Exception as e:
        # 网络错误/超时统一归为探活失败，不把异常抛给调用方
        return {"ok": False, "error": str(e)}
    finally:
        if owns_client:
            await client.aclose()
    return {"ok": False, "error": "unknown provider"}
