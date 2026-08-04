"""gateway-admin - FastAPI management API for MCP Gateway.

Serves the management API (/api/*) and the Vue 3 SPA (admin-ui/dist).
Shares Redis with gateway-proxy; writes servers/tokens, proxy hot-reloads.
"""
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from auth import verify_password, create_jwt, decode_jwt, ensure_default_admin
from redis_client import get_redis

from logging_config import configure_logging

configure_logging([
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.JSONRenderer(),
])
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
from api import servers, tokens, dashboard, keys
app.include_router(servers.router)
app.include_router(tokens.router)
app.include_router(dashboard.router)
app.include_router(keys.router)

# Serve Vue 3 SPA if dist exists (Plan C builds it).
# 不能用 StaticFiles 挂 "/"：它只对根路径返回 index.html，深层路由
# （/api-keys、/tokens）刷新会 404。改用 catch-all：真实静态文件优先
# 返回，否则回退 index.html 让前端路由接管（/api/* 已由上面 router 处理）。
_dist = os.path.join(os.path.dirname(__file__), "admin-ui", "dist")
_index_html = os.path.join(_dist, "index.html")


@app.get("/{full_path:path}")
async def spa_catch_all(full_path: str):
    # /api/* 已被上面的 router 匹配；走到这里的 /api/* 是 404，不回退 SPA
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    # 真实静态文件（/assets/index-xxx.js 等）直接返回
    candidate = os.path.join(_dist, full_path)
    if full_path and os.path.isfile(candidate):
        return FileResponse(candidate)
    # 其余路径（/、/api-keys、/servers 等前端路由）回退 index.html
    if os.path.isfile(_index_html):
        return FileResponse(_index_html)
    raise HTTPException(status_code=404, detail="admin-ui not built")
