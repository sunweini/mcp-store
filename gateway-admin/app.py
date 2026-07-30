"""gateway-admin - FastAPI management API for MCP Gateway.

Serves the management API (/api/*) and the Vue 3 SPA (admin-ui/dist).
Shares Redis with gateway-proxy; writes servers/tokens, proxy hot-reloads.
"""
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth import verify_password, create_jwt, decode_jwt, ensure_default_admin
from redis_client import get_redis

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
    await ensure_default_admin()
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


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(req: LoginRequest):
    r = get_redis()
    data = await r.hgetall(f"admin:{req.username}")
    if not data or not verify_password(req.password, data.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": create_jwt(req.username), "expires_in": 86400}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Routers
from api import servers
app.include_router(servers.router)
# TODO: tokens, dashboard routers in later tasks

# Serve Vue 3 SPA if dist exists (Plan C builds it)
_dist = os.path.join(os.path.dirname(__file__), "admin-ui", "dist")
if os.path.isdir(_dist):
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
