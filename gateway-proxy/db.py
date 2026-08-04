"""aiomysql 连接池单例。MySQL 专管调用审计（calls 表），Redis 管配置/状态。

连接串从 MYSQL_URL 解析（mysql://user:pass@host:port/db）。proxy 启动后首次
record_call 时懒加载池；池断线 aiomysql 自动重连。
"""
import os
from urllib.parse import urlparse

import aiomysql

_pool: aiomysql.Pool | None = None


def _parse_url(url: str) -> dict:
    p = urlparse(url)
    return {
        "host": p.hostname or "mysql",
        "port": p.port or 3306,
        "user": p.username or "mcp",
        "password": p.password or "",
        "db": (p.path or "/mcp_audit").lstrip("/"),
    }


async def get_pool() -> aiomysql.Pool:
    """懒加载连接池。首次调用创建，之后复用。"""
    global _pool
    if _pool is None:
        url = os.environ.get("MYSQL_URL", "")
        if not url:
            raise RuntimeError("MYSQL_URL not configured")
        cfg = _parse_url(url)
        _pool = await aiomysql.create_pool(
            minsize=2, maxsize=10, autocommit=True, **cfg,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
