"""Token 验证 + 账户级 read/write 校验（MCP 是账户级权限的权威）。

gateway 只做 server 级工具可见性粗闸；本模块基于 proxy 转发来的
Authorization 头验证 token（与 gateway 同一套 Redis tokens:{hash} 存储，
hash 算法一致），再查 AccountStore 的账户级权限做精细校验——这是
防御纵深：绕过 gateway 直连（部署禁止，容器不映射宿主）也会被拒。
"""
import hashlib

import structlog
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers

from account_store import AccountStore

logger = structlog.get_logger()


def hash_token(token: str) -> str:
    """SHA-256 hex digest（与 gateway-proxy auth.hash_token 一致）。"""
    return hashlib.sha256(token.encode()).hexdigest()


def extract_token(headers: dict | None) -> str | None:
    """从 headers 取 Bearer token；无 Authorization 头返回 None。"""
    if not headers:
        return None
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def _deny(error_type: str, message: str) -> ToolError:
    """统一错误构造：消息以 error_type 开头，client 可解析。"""
    return ToolError(f"permission denied: {error_type}: {message}")


class PermissionChecker:
    def __init__(self, store: AccountStore, redis):
        self._store = store
        self._redis = redis

    async def _token_id(self, headers: dict) -> str | None:
        """验证 Authorization token 并返回 token_id；无效返回 None。"""
        token = extract_token(headers)
        if not token:
            return None
        data = await self._redis.hgetall(f"tokens:{hash_token(token)}")
        if not data:
            return None
        return data.get("id")

    async def require(self, account_id: str, mode: str) -> None:
        """校验调用者对该账户有 mode（read/write）权限；失败 raise ToolError。

        read 判定为 read or write——write 隐含 read 是不变式，但 Redis 可能
        被手改出违规数据，这里防御式判定（spec §3.1）。
        """
        headers = get_http_headers(include_all=True)
        token_id = await self._token_id(headers)
        if not token_id:
            raise _deny("invalid_token", "missing or invalid Authorization token")
        await self._store.ensure_token_loaded(token_id)
        perms = self._store.get_token_perms(token_id)
        acct_perm = perms.get(account_id)
        if not acct_perm:
            raise _deny("no_permission", f"account '{account_id}' not granted to this token")
        allowed = bool(acct_perm.get("write")) if mode == "write" else bool(acct_perm.get("read") or acct_perm.get("write"))
        if not allowed:
            raise _deny("no_permission", f"token lacks {mode} permission on account '{account_id}'")

    async def allowed_accounts(self) -> list[dict]:
        """当前 token 可访问的账户清单（list_accounts 用），含 read/write 标记。"""
        headers = get_http_headers(include_all=True)
        token_id = await self._token_id(headers)
        if not token_id:
            raise _deny("invalid_token", "missing or invalid Authorization token")
        await self._store.ensure_token_loaded(token_id)
        out = []
        for account_id, perm in self._store.get_token_perms(token_id).items():
            creds = self._store.get_credentials(account_id)
            if not creds or not creds["enabled"]:
                continue  # 只列托管中且启用的账户
            out.append({
                "account_id": account_id,
                "description": creds["description"],
                "read": bool(perm.get("read") or perm.get("write")),
                "write": bool(perm.get("write")),
            })
        return out
