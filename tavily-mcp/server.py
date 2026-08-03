"""Tavily MCP Server — multi-key search via Tavily API.

Env vars:
- REDIS_URL (必填): Redis 连接，key 池从这里读（如 redis://redis:6379/0）
- MCP_HOST / MCP_PORT (默认 127.0.0.1 / 9050)
- LOG_FORMAT: console|json
- PROMETHEUS_PORT (默认 9464)
- TAVILY_QUOTA_DEFAULT (默认 1000): 未设置 monthly_quota 时默认月配额
"""
import asyncio
import os

import structlog
from fastmcp import FastMCP
import redis.asyncio as redis

from key_pool import KeyPool

MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "9050"))
REDIS_URL = os.environ.get("REDIS_URL", "")
QUOTA_DEFAULT = int(os.environ.get("TAVILY_QUOTA_DEFAULT", "1000"))
LOG_FORMAT = os.environ.get("LOG_FORMAT", "console")

logger = structlog.get_logger()


def _configure_logging() -> None:
    from logging_config import configure_logging
    configure_logging([
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer() if LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(),
    ])


# Process-level pool, initialized at module load (stateless mode).
_pool = None


def _init_pool() -> KeyPool:
    global _pool
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL environment variable is required")
    client = redis.from_url(REDIS_URL, decode_responses=True)
    pubsub = client.pubsub()
    _pool = KeyPool("tavily", client, pubsub, quota_default=QUOTA_DEFAULT)
    return _pool


def _get_pool() -> KeyPool:
    if _pool is None:
        raise RuntimeError("KeyPool not initialized")
    return _pool


_configure_logging()

# OTel (no-op if SDK not installed)
try:
    from telemetry import init_telemetry
    init_telemetry("tavily-mcp")
except ImportError:
    pass

mcp = FastMCP(
    "Tavily MCP",
    instructions=(
        "Search tools backed by Tavily API with automatic API key rotation. "
        "Start with tavily_search for general queries. "
        "tavily_extract pulls clean content from URLs; tavily_research is a "
        "long-running deep research task. All tools are read-only."
    ),
)

from tools import register_tools
register_tools(mcp, _get_pool)


async def _start_pool_listener():
    """Start the Pub/Sub hot-reload listener in the background."""
    pool = _get_pool()
    # KeyPool.start() 假定 pubsub 已订阅热更新频道；漏掉这句热更新会
    # 静默失效（监听循环收不到消息），是 Redis 部署最难查的问题之一
    await pool._pubsub.subscribe("search:keys:channel")
    await pool.start()


if __name__ == "__main__":
    _init_pool()
    asyncio.run(_start_pool_listener())
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        host=MCP_HOST,
        port=MCP_PORT,
    )
