"""Aliyun DNS MCP Server — entry point.

提供阿里云 DNS 解析管理：多账户托管、域名/解析查询、增删改解析记录。
账户级 read/write 权限由本 server 校验（MCP 是权威）：gateway 的 proxy
transport 自动转发 Authorization 头，本服务验证 token 并查账户级权限。

Observability: structlog + OTel（日志注入 trace_id/span_id）+ Prometheus。
Env: REDIS_URL（必填）、MCP_HOST/MCP_PORT、LOG_FORMAT、PROMETHEUS_PORT、
OTEL_EXPORTER_OTLP_ENDPOINT、OTEL_SERVICE_NAME。
"""
import asyncio
import os

import structlog
from fastmcp import FastMCP
import redis.asyncio as redis

from account_store import AccountStore
from auth import PermissionChecker
from aliyun_client import ClientFactory
from tools import ToolContext

MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "9054"))
REDIS_URL = os.environ.get("REDIS_URL", "")
LOG_FORMAT = os.environ.get("LOG_FORMAT", "console")


def _configure_logging() -> None:
    from logging_config import configure_logging
    configure_logging([
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer() if LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(),
    ])


logger = structlog.get_logger()

# 进程级单例（stateless 模式 lifespan 不可靠，模块级 init）。
_store = None
_checker = None
_clients = None


def _init_runtime() -> None:
    """初始化 AccountStore/PermissionChecker/ClientFactory。启动时调用一次。"""
    global _store, _checker, _clients
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL environment variable is required")
    client = redis.from_url(REDIS_URL, decode_responses=True)
    _store = AccountStore(client)
    _checker = PermissionChecker(_store, client)
    _clients = ClientFactory(_store)


def _get_ctx() -> ToolContext:
    if _checker is None or _clients is None:
        raise RuntimeError("runtime not initialized — call _init_runtime()")
    return ToolContext(checker=_checker, clients=_clients)


_configure_logging()

try:
    from telemetry import init_telemetry
    init_telemetry("aliyun-dns-mcp")
except Exception as exc:
    # 可观测性降级不应杀服务
    logger.warning("telemetry_init_failed", service="aliyun-dns-mcp", error=str(exc))

mcp = FastMCP(
    "Aliyun DNS MCP",
    instructions=(
        "阿里云 DNS 解析管理：list_accounts 查看可访问账户，"
        "list_domains/list_records 查询，add_record/update_record/delete_record "
        "增删改解析记录。所有写操作需要用户确认，且受账户级读写权限控制。"
    ),
)

from tools import register_tools
register_tools(mcp, _get_ctx)


if __name__ == "__main__":
    _init_runtime()

    async def _run() -> None:
        # listener 必须与 server 同 event loop（serpapi 教训：跨 loop 用
        # redis 连接直接 RuntimeError）
        await _store.start()
        await mcp.run_async(
            transport="streamable-http",
            stateless_http=True,
            host=MCP_HOST,
            port=MCP_PORT,
        )

    asyncio.run(_run())
