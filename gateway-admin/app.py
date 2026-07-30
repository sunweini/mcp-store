"""gateway-admin - FastAPI management API for MCP Gateway.

Serves the management API (/api/*) and the Vue 3 SPA (admin-ui/dist).
Shares Redis with gateway-proxy; writes servers/tokens, proxy hot-reloads.
"""
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)
logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app):
    logger.info("admin_started", service="gateway-admin")
    yield
    from redis_client import close_redis
    await close_redis()
    logger.info("admin_stopped", service="gateway-admin")


app = FastAPI(title="MCP Gateway Admin", lifespan=lifespan)

# CORS for Vue dev server (localhost:5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8081"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Routers added in later tasks:
# from api import servers, tokens, dashboard
# app.include_router(servers.router)
# ...

# Serve Vue 3 SPA if dist exists (Plan C builds it)
_dist = os.path.join(os.path.dirname(__file__), "admin-ui", "dist")
if os.path.isdir(_dist):
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
