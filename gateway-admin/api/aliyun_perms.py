"""token×账户 read/write 授权矩阵 API。

Owns aliyndns:token_accounts:{token_id}（账户级权限权威，MCP 读取执行）。
保存时计算 union 同步 gateway token（tokens:{hash}）的 aliyun-dns-mcp
read/write——保证有任一账户写权限的 token 能看到写工具（gateway 的
工具可见性粗闸，spec §3.2）。
"""
import json

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin
from redis_client import get_redis

logger = structlog.get_logger()

router = APIRouter(prefix="/api/aliyun-perms", tags=["aliyun-perms"])

CHANGE_CHANNEL = "aliyndns:changed"
SERVER_NAME = "aliyun-dns-mcp"


class PermSpec(BaseModel):
    read: bool = False
    write: bool = False


class PermsPut(BaseModel):
    permissions: dict[str, PermSpec]


async def _publish(token_id: str) -> None:
    try:
        r = get_redis()
        await r.publish(CHANGE_CHANNEL, json.dumps(
            {"action": "upsert", "key": f"aliyndns:token_accounts:{token_id}"}))
    except Exception as e:
        logger.warning("aliyun_perm_publish_failed", token_id=token_id,
                       error=str(e), service="gateway-admin")


async def _recompute_union(r, token_id: str, remaining: dict[str, dict]) -> None:
    """按剩余账户授权重算 gateway token 的 server 级 read/write union。

    aliyndns:token_accounts:{token_id}（账户级权限权威）与 gateway token
    的 tokens:{hash}.permissions.aliyun-dns-mcp（工具可见性粗闸）双写必须
    一致：授权变更（put_perms）与账户删除（delete_account 清理引用）都经
    此收敛，否则管理界面 server 级权限与授权矩阵互相矛盾。
    token 已不存在（token_id 无映射）则跳过——delete_token 已整体清理
    授权映射，无需重算。
    """
    token_hash = await r.get(f"token_id:{token_id}")
    if not token_hash:
        return
    perms = json.loads((await r.hgetall(f"tokens:{token_hash}")).get("permissions", "{}"))
    perms[SERVER_NAME] = {
        "read": any(v.get("read") for v in remaining.values()),
        "write": any(v.get("write") for v in remaining.values()),
    }
    await r.hset(f"tokens:{token_hash}", "permissions", json.dumps(perms))


@router.get("/{token_id}")
async def get_perms(token_id: str, _: str = Depends(require_admin)):
    r = get_redis()
    if not await r.get(f"token_id:{token_id}"):
        raise HTTPException(status_code=404, detail="token not found")
    mapping = {}
    for account_id, payload in (await r.hgetall(f"aliyndns:token_accounts:{token_id}")).items():
        try:
            mapping[account_id] = json.loads(payload)
        except json.JSONDecodeError:
            continue  # 脏数据不暴露，跳过（keys.py 同策略）
    return {"token_id": token_id, "permissions": mapping}


@router.put("/{token_id}")
async def put_perms(token_id: str, req: PermsPut, _: str = Depends(require_admin)):
    r = get_redis()
    token_hash = await r.get(f"token_id:{token_id}")
    if not token_hash:
        raise HTTPException(status_code=404, detail="token not found")
    # 校验账户存在 + 强制 write⇒read 不变式
    normalized = {}
    for account_id, p in req.permissions.items():
        if not await r.exists(f"aliyndns:accounts:{account_id}"):
            raise HTTPException(status_code=422, detail=f"account '{account_id}' not managed")
        write = bool(p.write)
        normalized[account_id] = {"read": bool(p.read) or write, "write": write}
    if normalized:
        await r.hset(f"aliyndns:token_accounts:{token_id}",
                     mapping={a: json.dumps(v, ensure_ascii=False) for a, v in normalized.items()})
    else:
        await r.delete(f"aliyndns:token_accounts:{token_id}")
    # union → gateway token 的 server 级 read/write（工具可见性）
    await _recompute_union(r, token_id, normalized)
    await _publish(token_id)
    logger.info("aliyun_perms_updated", token_id=token_id, accounts=len(normalized),
                service="gateway-admin")
    return {"token_id": token_id, "permissions": normalized}
