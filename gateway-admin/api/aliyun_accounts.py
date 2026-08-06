"""阿里云 DNS 账户管理 API。

Owns aliyndns:accounts:* Redis keys；aliyun-dns-mcp 读这些 key 做账户
凭证与热更新。写 → PUBLISH aliyndns:changed 让 MCP 免重启刷新。
AccessKey/Secret 明文只存内网 Redis（与 gateway token 存储同策略），
探活用 SDK 后丢弃，响应只回 account_id/description/掩码，明文永不回传。
"""
import json
import time

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import require_admin
from redis_client import get_redis

logger = structlog.get_logger()

router = APIRouter(prefix="/api/aliyun-accounts", tags=["aliyun-accounts"])

ACCOUNTS_INDEX = "aliyndns:accounts:index"
CHANGE_CHANNEL = "aliyndns:changed"


class AccountCreate(BaseModel):
    account_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9-]+$")
    description: str = ""
    access_key_id: str = Field(min_length=1)
    access_key_secret: str = Field(min_length=1)
    region: str = "cn-hangzhou"
    enabled: bool = True
    probe: bool = True


class AccountUpdate(BaseModel):
    description: str | None = None
    access_key_id: str | None = None
    access_key_secret: str | None = None
    region: str | None = None
    enabled: bool | None = None
    probe: bool = True


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _mask(key_id: str) -> str:
    if len(key_id) <= 12:
        return key_id[:4] + "…"
    return f"{key_id[:4]}…{key_id[-4:]}"


def _safe_probe_error(code: str, msg: str) -> str:
    """探活错误 → 落 Redis probe_error 的安全文本。

    为什么裁剪：网络异常消息可能含完整请求 URL（requests ConnectionError
    消息带 "GET https://.../?AccessKeyId=<明文>&Signature=..."），而
    probe_error 会被落 Redis 并在列表接口/前端展示——任何带凭证的内容
    都是泄漏（spec §8.1 敏感防线）。策略与 MCP 侧 _call 一致：到 "?" 即截断
    （query 全是签名参数，无诊断价值），主机名保留。
    """
    return (code or msg[:200]).split("?", 1)[0]


async def _publish(action: str, account_id: str) -> None:
    """PUBLISH 变更通知让 MCP 热更新。主操作已成功即成功，publish 失败只 warning。"""
    try:
        r = get_redis()
        await r.publish(CHANGE_CHANNEL, json.dumps(
            {"action": action, "key": f"aliyndns:accounts:{account_id}"}))
    except Exception as e:
        logger.warning("aliyun_account_publish_failed", account_id=account_id,
                       error=str(e), service="gateway-admin")


async def _probe(access_key_id: str, access_key_secret: str, region: str) -> dict:
    """用该凭证调 DescribeDomains(PageSize=1) 验证有效性（查询免费）。

    同步 SDK → asyncio.to_thread；失败返回 {"ok": False, "error": 原因}。
    """
    try:
        import asyncio
        from alibabacloud_tea_openapi import models as open_api_models
        from alibabacloud_alidns20150109 import client as alidns_client
        from alibabacloud_alidns20150109 import models as alidns_models

        def run():
            c = alidns_client.Client(open_api_models.Config(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                endpoint="alidns.cn-hangzhou.aliyuncs.com",
            ))
            c.describe_domains_with_options(
                alidns_models.DescribeDomainsRequest(page_size=1, page_number=1), {})

        await asyncio.to_thread(run)
        return {"ok": True}
    except Exception as e:
        code = getattr(e, "code", "")
        # probe_error 会落 Redis + 前端展示：必须裁剪可能含凭证的 URL query
        return {"ok": False, "error": _safe_probe_error(code, str(e))}


@router.get("")
async def list_accounts(_: str = Depends(require_admin)):
    r = get_redis()
    out = []
    for account_id in await r.smembers(ACCOUNTS_INDEX):
        data = await r.hgetall(f"aliyndns:accounts:{account_id}")
        if not data:
            continue
        out.append({
            "account_id": account_id,
            "description": data.get("description", ""),
            "region": data.get("region", "cn-hangzhou"),
            "enabled": data.get("enabled", "true") == "true",
            "access_key_masked": _mask(data.get("access_key_id", "")),
            "probe_error": data.get("probe_error"),
            "created_at": data.get("created_at", ""),
        })
    return out


@router.post("", status_code=201)
async def create_account(req: AccountCreate, _: str = Depends(require_admin)):
    r = get_redis()
    if await r.exists(f"aliyndns:accounts:{req.account_id}"):
        raise HTTPException(status_code=422, detail="account_id 已存在")
    probe_error = None
    if req.probe:
        result = await _probe(req.access_key_id, req.access_key_secret, req.region)
        if not result["ok"]:
            # 探活失败不阻断添加（管理员可能先入库后修复）；错误提示前台可见
            probe_error = result["error"]
    await r.hset(f"aliyndns:accounts:{req.account_id}", mapping={
        "access_key_id": req.access_key_id,
        "access_key_secret": req.access_key_secret,
        "description": req.description,
        "region": req.region,
        "enabled": "true" if req.enabled else "false",
        "probe_error": probe_error or "",
        "created_at": _now_iso(),
    })
    await r.sadd(ACCOUNTS_INDEX, req.account_id)
    await _publish("upsert", req.account_id)
    logger.info("aliyun_account_created", account_id=req.account_id, service="gateway-admin")
    return {
        "account_id": req.account_id,
        "description": req.description,
        "region": req.region,
        "enabled": req.enabled,
        "probe_error": probe_error,
    }


@router.put("/{account_id}")
async def update_account(account_id: str, req: AccountUpdate, _: str = Depends(require_admin)):
    r = get_redis()
    if not await r.exists(f"aliyndns:accounts:{account_id}"):
        raise HTTPException(status_code=404, detail="account not found")
    data = await r.hgetall(f"aliyndns:accounts:{account_id}")
    updates = {}
    if req.description is not None:
        updates["description"] = req.description
    if req.access_key_id is not None:
        updates["access_key_id"] = req.access_key_id
    if req.access_key_secret is not None:
        updates["access_key_secret"] = req.access_key_secret
    if req.region is not None:
        updates["region"] = req.region
    if req.enabled is not None:
        updates["enabled"] = "true" if req.enabled else "false"
    # 凭证变更时探活（可选，默认开）
    probe_error = data.get("probe_error")
    if req.probe and (req.access_key_id or req.access_key_secret):
        result = await _probe(
            updates.get("access_key_id", data.get("access_key_id", "")),
            updates.get("access_key_secret", data.get("access_key_secret", "")),
            updates.get("region", data.get("region", "cn-hangzhou")),
        )
        probe_error = None if result["ok"] else result["error"]
    updates["probe_error"] = probe_error or ""
    if updates:
        await r.hset(f"aliyndns:accounts:{account_id}", mapping=updates)
    await _publish("upsert", account_id)
    logger.info("aliyun_account_updated", account_id=account_id, service="gateway-admin")
    return {
        "account_id": account_id,
        "description": updates.get("description", data.get("description", "")),
        "enabled": updates.get("enabled", data.get("enabled", "true")) == "true",
        "probe_error": probe_error,
    }


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: str, _: str = Depends(require_admin)):
    r = get_redis()
    removed = await r.delete(f"aliyndns:accounts:{account_id}")
    if not removed:
        raise HTTPException(status_code=404, detail="account not found")
    await r.srem(ACCOUNTS_INDEX, account_id)
    # 清理授权引用：删除账户时从所有 token 的授权映射移除该账户，并重算
    # 各 token 的 server 级 union（防僵尸授权 + 权限残留——MCP 侧虽有
    # account_not_found 兜底，数据应保持干净，spec §6.2）
    from api.aliyun_perms import _recompute_union
    async for key in r.scan_iter(match="aliyndns:token_accounts:*"):
        token_id = key.rsplit(":", 1)[-1]
        await r.hdel(key, account_id)
        # 剩余授权非空：重算 union；空 hash：删 key + union 归零（唯一授权
        # 被删时 server 级权限必须同步降为 read=false/write=false）
        remaining = await r.hgetall(key)
        if remaining:
            # 脏 payload（非 JSON）跳过，不影响其余账户的 union 计算
            perms = {}
            for acct, payload in remaining.items():
                try:
                    perms[acct] = json.loads(payload)
                except json.JSONDecodeError:
                    continue
            await _recompute_union(r, token_id, perms)
        else:
            await r.delete(key)
            await _recompute_union(r, token_id, {})
    await _publish("delete", account_id)
    logger.info("aliyun_account_deleted", account_id=account_id, service="gateway-admin")
    return None
