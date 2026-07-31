"""MCP Gateway Proxy - entry point.

Aggregates backend MCP servers via FastMCP mount(namespace=...). Validates
API tokens (SHA-256 vs Redis), enforces per-server read/write via middleware,
records failures to a Redis Stream, and exposes Prometheus metrics.

Startup uses the FastMCP lifespan pattern (not a separate event loop) so
the Redis client + background tasks live in the same loop that serves
requests. See knowledge-base/fastmcp-v4/63-lifespan.md.
"""
import os
from contextlib import asynccontextmanager

import structlog
from fastmcp import FastMCP

from observability import init_telemetry
from permission_middleware import PermissionMiddleware
from redis_client import close_redis
from registry import mount_all, watch_changes

from logging_config import configure_logging

configure_logging([
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.JSONRenderer(),
])

GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8080"))

logger = structlog.get_logger()


@asynccontextmanager
async def gateway_lifespan(server):
    """Startup: mount backends from Redis + start the hot-reload watcher.

    FastMCP's run_http_async calls _lifespan_manager() which enters this
    context before starting Uvicorn. The background task (watch_changes)
    runs in the same event loop as the HTTP server, so Redis pubsub
    callbacks can safely call mount/unmount on the gateway.

    Why not asyncio.new_event_loop (as the original brief suggested):
    mcp.run() manages its own loop via anyio.run(); objects created in a
    different loop (Redis connection, asyncio tasks) are not usable from
    the server's loop. The lifespan pattern is the FastMCP-blessed way to
    run startup code inside the server's event loop.
    """
    # Initialize telemetry at server startup (not import time) so that:
    # 1. Tests importing server.py don't trigger the Prometheus HTTP server.
    # 2. The meter/tracer provider is ready before mount_all uses the modules.
    init_telemetry()
    await mount_all(server)
    # Start the pubsub watcher as a fire-and-forget background task.
    # It runs for the lifetime of the server; we don't await it.
    import asyncio
    watcher = asyncio.create_task(watch_changes(server))
    logger.info("gateway_started", port=GATEWAY_PORT, service="gateway-proxy")
    try:
        yield
    finally:
        watcher.cancel()
        await close_redis()
        logger.info("gateway_stopped", service="gateway-proxy")


gateway = FastMCP(
    "MCP Gateway",
    instructions="Aggregates backend MCP servers. Token auth required (Authorization: Bearer).",
    lifespan=gateway_lifespan,
)

# Add the permission middleware - it intercepts tools/call only.
gateway.add_middleware(PermissionMiddleware())


if __name__ == "__main__":
    gateway.run(
        transport="streamable-http",
        stateless_http=True,
        host=os.environ.get("GATEWAY_HOST", "0.0.0.0"),
        port=GATEWAY_PORT,
    )
