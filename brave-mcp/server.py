"""Brave MCP Server — multi-key search via Brave Search API.

Env vars:
- REDIS_URL (必填): Redis 连接，key 池从这里读（如 redis://redis:6379/0）
- MCP_HOST / MCP_PORT (默认 127.0.0.1 / 9051)
- LOG_FORMAT: console|json
- PROMETHEUS_PORT (默认 9464)
- BRAVE_QUOTA_DEFAULT (默认 2000): 未设置 monthly_quota 时默认月配额
"""
import asyncio
import os

import structlog
from fastmcp import FastMCP
import redis.asyncio as redis

from key_pool import KeyPool

MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "9051"))
REDIS_URL = os.environ.get("REDIS_URL", "")
QUOTA_DEFAULT = int(os.environ.get("BRAVE_QUOTA_DEFAULT", "2000"))
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
    _pool = KeyPool("brave", client, pubsub, quota_default=QUOTA_DEFAULT)
    return _pool


def _get_pool() -> KeyPool:
    if _pool is None:
        raise RuntimeError("KeyPool not initialized")
    return _pool


_configure_logging()

# OTel (metrics 降级不应杀服务——init_telemetry 内部已是 except Exception，
# 这里用宽异常保持对称（I4）；zabbix 无 except 直接裸导入，此处更稳)
try:
    from telemetry import init_telemetry
    init_telemetry("brave-mcp")
except Exception as exc:
    logger.warning("telemetry_init_failed", service="brave-mcp", error=str(exc))

mcp = FastMCP(
    "Brave MCP",
    instructions=(
        "Search tools backed by Brave Search API with automatic API key "
        "rotation. Start with brave_web_search for general web queries; "
        "brave_local_search finds local business/place results. "
        "All tools are read-only."
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

    async def _run() -> None:
        # C1 修复：listener 与 server 必须在同一 event loop。旧写法
        # `asyncio.run(_start_pool_listener())` 先跑一个短命 loop——它
        # 返回即关闭 loop 并 cancel 所有遗留任务（pool._listen_task 被
        # 取消），随后 mcp.run() 在 anyio 新建的 loop 里用同一 redis
        # 连接，跨 loop 使用直接抛 RuntimeError。现改为单 loop：
        # _start_pool_listener 内部订阅+start 快速返回，listener 随
        # server loop 存活。
        await _start_pool_listener()
        await mcp.run_async(
            transport="streamable-http",
            stateless_http=True,
            host=MCP_HOST,
            port=MCP_PORT,
        )

    asyncio.run(_run())
