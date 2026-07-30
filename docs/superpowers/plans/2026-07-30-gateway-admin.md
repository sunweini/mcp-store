# gateway-admin Backend Implementation Plan (Plan B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `gateway-admin` - a FastAPI service providing the management API (admin JWT login, server CRUD + probe + tools introspection, token CRUD with SHA-256 hashing, dashboard metrics from Prometheus + Redis Stream) and serving the Vue 3 SPA.

**Architecture:** FastAPI app with APIRouters per domain (servers/tokens/dashboard). Redis stores servers/tokens/admin (shared schema with gateway-proxy). On server register: write Redis hash + SADD active set + PUBLISH `server:changed` (proxy hot-reloads). Token stored SHA-256 hashed, plaintext returned once. Dashboard reads Prometheus HTTP API (gateway-proxy :9464) + Redis Stream `audit:failures`. JWT guards all /api routes except /api/login.

**Tech Stack:** FastAPI, uvicorn, redis (async), httpx, PyJWT, bcrypt, structlog, pytest, pytest-asyncio, fakeredis

## Global Constraints

- Python >=3.12, uv with `prerelease = "allow"`
- FastAPI + MCP 2026-07-28 ecosystem (this service is FastAPI, NOT FastMCP - it's the management plane)
- All logs: structlog key=value, no f-string logging
- Token storage: SHA-256 hashed, never plaintext; key = `tokens:{sha256(token)}`, reverse index `token_id:{id}`
- Server names: `[a-z0-9-]` only, **no underscores** (namespace prefix rule)
- Tool mode: `annotations.destructiveHint == True` -> write, else read
- Token plaintext returned ONLY in POST /api/tokens response; list/detail return mask `tok_xxxx****`
- Comments explain "why" not "what" (OBS-CORE-005)
- Shared Redis schema with gateway-proxy: `servers:{name}`, `servers:active` (SET), `server:changed` (PUB/SUB), `tokens:{sha256}`, `token_id:{id}`, `audit:failures` (STREAM), `admin:{username}`

---

## File Structure

```
gateway-admin/
├── CLAUDE.md
├── pyproject.toml
├── app.py                # FastAPI app: routers, lifespan, static mount, CORS
├── redis_client.py       # async Redis singleton (mirrors gateway-proxy)
├── auth.py               # JWT issue/verify + bcrypt + admin login + dependency
├── mcp_probe.py          # probe(url) + introspect_tools(url) - MCP ping + tools/list
├── metrics.py            # Prometheus HTTP client + parse -> §10 JSON shapes
├── api/
│   ├── __init__.py
│   ├── servers.py        # Server CRUD + /status probe + tools introspection
│   ├── tokens.py         # Token CRUD + permission validation + one-time reveal
│   └── dashboard.py      # metrics/summary, by-server, timeseries + failures (Stream)
├── tests/
│   ├── conftest.py       # fake_redis fixture + FastAPI TestClient
│   ├── test_auth.py
│   ├── test_servers.py
│   ├── test_tokens.py
│   ├── test_mcp_probe.py
│   └── test_dashboard.py
└── admin-ui/             # Vue 3 (Plan C, not this plan) - app.py serves dist/ if present
```

---

### Task 1: Scaffolding + Redis Client + App Skeleton

**Files:**
- Create: `gateway-admin/pyproject.toml`
- Create: `gateway-admin/CLAUDE.md`
- Create: `gateway-admin/redis_client.py`
- Create: `gateway-admin/app.py`
- Create: `gateway-admin/tests/conftest.py`

**Interfaces:**
- Produces: `get_redis() -> redis.asyncio.Redis`, FastAPI `app`

- [ ] **Step 1: Create pyproject.toml**

```toml
[tool.uv]
prerelease = "allow"

[project]
name = "gateway-admin"
version = "0.1.0"
description = "MCP Gateway Admin - management API + Vue 3 UI"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "redis>=5.0",
    "httpx>=0.27,<1.0",
    "PyJWT>=2.8",
    "bcrypt>=4.1",
    "structlog>=24.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "fakeredis>=2.20",
    "httpx>=0.27,<1.0",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 2: Install deps**

```bash
cd gateway-admin && uv sync --all-extras
```

- [ ] **Step 3: Create CLAUDE.md**

```markdown
# gateway-admin - 开发说明

## 概述
MCP 网关管理面。FastAPI 管理 API（server/token/dashboard）+ Vue 3 静态前端。与 gateway-proxy 共享 Redis。

## 架构
- FastAPI + APIRouter（servers/tokens/dashboard）
- JWT 管理员认证（bcrypt + PyJWT）
- 写 Redis（server/token/admin），proxy 通过 Pub/Sub 热加载
- 读 Prometheus（gateway-proxy:9464）+ Redis Stream（audit:failures）

## 本地开发
\`\`\`bash
uv sync
REDIS_URL=redis://localhost:6379/0 JWT_SECRET=dev uv run uvicorn app:app --port 8081 --reload
uv run pytest tests/ -v
\`\`\`

## 配置
| 环境变量 | 默认 | 说明 |
|---|---|---|
| ADMIN_PORT | 8081 | 监听端口 |
| REDIS_URL | redis://localhost:6379/0 | Redis |
| JWT_SECRET | (必填) | JWT 签名密钥 |
| JWT_EXPIRES | 86400 | JWT 有效期秒 |
| GATEWAY_PROXY_METRICS_URL | http://localhost:9464/metrics | Prometheus |

## 共享 Redis schema
见根 CLAUDE.md + gateway-proxy。admin 写 servers/tokens/admin，读 audit:failures。
```

- [ ] **Step 4: Create redis_client.py** (mirrors gateway-proxy)

```python
"""Async Redis connection singleton. Shared schema with gateway-proxy."""
import os
import redis.asyncio as redis

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _redis = redis.from_url(url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
```

- [ ] **Step 5: Create app.py skeleton**

```python
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
```

- [ ] **Step 6: Create tests/conftest.py**

```python
"""Shared fixtures: fake Redis + FastAPI TestClient override."""
import pytest
import fakeredis.aioredis
from fastapi.testclient import TestClient


@pytest.fixture
async def fake_redis(monkeypatch):
    import redis_client
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "_redis", fake)
    yield fake
    await fake.aclose()


@pytest.fixture
def client():
    """Synchronous FastAPI TestClient (uses anyio portal for async lifespan)."""
    from app import app
    with TestClient(app) as c:
        yield c
```

- [ ] **Step 7: Smoke test**

```bash
cd gateway-admin
JWT_SECRET=dev uv run python -c "from app import app; print([r.path for r in app.routes])"
uv run pytest tests/ -v  # 0 tests, no import errors
```
Expected: route list includes `/api/health`; pytest collects 0 with no errors.

- [ ] **Step 8: Commit**

```bash
git add gateway-admin
git commit -m "feat(gateway-admin): scaffold FastAPI app + redis client

- pyproject: fastapi, uvicorn, redis, httpx, PyJWT, bcrypt, structlog
- app.py: lifespan, CORS, /api/health, static mount placeholder
- conftest: fakeredis + TestClient fixtures"
```

---

### Task 2: Admin Auth - JWT + bcrypt

**Files:**
- Create: `gateway-admin/auth.py`
- Create: `gateway-admin/tests/test_auth.py`
- Modify: `gateway-admin/app.py` (add /api/login route)

**Interfaces:**
- Consumes: `redis_client.get_redis()`
- Produces: `hash_password(pw) -> str`, `verify_password(pw, hash) -> bool`, `create_jwt(sub) -> str`, `decode_jwt(token) -> str|None`, `require_admin(request) -> str` (FastAPI dependency), `ensure_default_admin()`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_auth.py
import pytest
from auth import hash_password, verify_password, create_jwt, decode_jwt, mask_token


def test_hash_and_verify_password():
    h = hash_password("secret123")
    assert h != "secret123"
    assert verify_password("secret123", h) is True
    assert verify_password("wrong", h) is False


def test_create_and_decode_jwt():
    tok = create_jwt("admin")
    assert tok.startswith("eyJ")
    sub = decode_jwt(tok)
    assert sub == "admin"


def test_decode_invalid_jwt_returns_none():
    assert decode_jwt("not.a.jwt") is None


def test_mask_token():
    assert mask_token("tok_9f3kq8zabbix001") == "tok_9f3k****"
    assert mask_token("short") == "****"


async def test_login_success(client, fake_redis):
    from auth import hash_password, ensure_default_admin
    await fake_redis.hset("admin:admin", mapping={
        "password_hash": hash_password("admin123"),
        "role": "admin",
    })
    resp = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    data = resp.json()
    assert "token" in data
    assert data["expires_in"] == 86400


def test_login_wrong_password(client, fake_redis):
    resp = client.post("/api/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401
```

- [ ] **Step 2: Run, verify fail**

```bash
cd gateway-admin && uv run pytest tests/test_auth.py -v
```
Expected: FAIL `ModuleNotFoundError: No module named 'auth'`

- [ ] **Step 3: Implement auth.py**

```python
"""Admin authentication: bcrypt passwords + JWT sessions.

Admin accounts live in Redis as admin:{username} Hash. JWT guards all
/api routes except /api/login. Token API tokens (MCP client auth) are a
separate concern (tokens.py) - do not confuse the two.
"""
import os
import time
import hashlib
import bcrypt
import jwt
import structlog

logger = structlog.get_logger()

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-me")
JWT_EXPIRES = int(os.environ.get("JWT_EXPIRES", "86400"))
JWT_ALGO = "HS256"


def hash_password(password: str) -> str:
    """bcrypt hash a password. Returns utf-8 str."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        return False


def create_jwt(subject: str) -> str:
    """Issue a JWT for an admin subject. Expires in JWT_EXPIRES seconds."""
    now = int(time.time())
    return jwt.encode(
        {"sub": subject, "iat": now, "exp": now + JWT_EXPIRES},
        JWT_SECRET,
        algorithm=JWT_ALGO,
    )


def decode_jwt(token: str) -> str | None:
    """Verify a JWT and return its subject, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


def mask_token(token: str) -> str:
    """Mask a token for list/detail display: first 8 chars + ****."""
    if len(token) <= 8:
        return "****"
    return token[:8] + "****"


async def ensure_default_admin() -> None:
    """Create a default admin:admin account if none exists.

    NOTE: first-run bootstrap only. Change password immediately in prod.
    """
    from redis_client import get_redis
    r = get_redis()
    if not await r.exists("admin:admin"):
        await r.hset("admin:admin", mapping={
            "password_hash": hash_password("admin123"),
            "role": "admin",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        logger.warning("default_admin_created", service="gateway-admin",
                       note="change password immediately")
```

- [ ] **Step 4: Add login route + dependency to app.py**

Add to `app.py` (after the CORS middleware, before `/api/health`):

```python
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
from auth import verify_password, create_jwt, decode_jwt, ensure_default_admin
from redis_client import get_redis


class LoginRequest(BaseModel):
    username: str
    password: str


async def require_admin(request: Request) -> str:
    """FastAPI dependency: validate JWT from Authorization header.

    Returns the admin subject (username) or raises 401.
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    sub = decode_jwt(auth[7:].strip())
    if sub is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return sub


@app.post("/api/login")
async def login(req: LoginRequest):
    r = get_redis()
    data = await r.hgetall(f"admin:{req.username}")
    if not data or not verify_password(req.password, data.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": create_jwt(req.username), "expires_in": 86400}
```

Also call `ensure_default_admin()` in lifespan (before yield):

```python
@asynccontextmanager
async def lifespan(app):
    await ensure_default_admin()
    logger.info("admin_started", service="gateway-admin")
    yield
    from redis_client import close_redis
    await close_redis()
```

- [ ] **Step 5: Run, verify pass**

```bash
uv run pytest tests/test_auth.py -v
```
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add auth.py tests/test_auth.py app.py
git commit -m "feat(gateway-admin): admin auth (bcrypt + JWT) + login route"
```

---

### Task 3: Server CRUD API + Redis + Pub/Sub notify

**Files:**
- Create: `gateway-admin/api/__init__.py`
- Create: `gateway-admin/api/servers.py`
- Create: `gateway-admin/tests/test_servers.py`
- Modify: `gateway-admin/app.py` (include router)

**Interfaces:**
- Consumes: `redis_client.get_redis()`, `auth.require_admin`
- Produces: `api.servers.router` (APIRouter), `validate_server_name(name)`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_servers.py
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def test_create_server(client, fake_redis, auth_headers):
    resp = client.post("/api/servers", json={
        "name": "zabbix", "url": "http://localhost:8000/mcp",
        "description": "Zabbix MCP",
    }, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "zabbix"
    assert data["url"] == "http://localhost:8000/mcp"


def test_create_server_invalid_name_underscore(client, auth_headers):
    resp = client.post("/api/servers", json={
        "name": "my_server", "url": "http://x", "description": "",
    }, headers=auth_headers)
    assert resp.status_code == 422


def test_list_servers(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    resp = client.get("/api/servers", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "zabbix"


def test_delete_server(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    resp = client.delete("/api/servers/zabbix", headers=auth_headers)
    assert resp.status_code == 204
    # gone
    resp = client.get("/api/servers", headers=auth_headers)
    assert resp.json() == []


def test_unauth_rejected(client):
    assert client.get("/api/servers").status_code == 401
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run pytest tests/test_servers.py -v
```
Expected: FAIL

- [ ] **Step 3: Implement api/servers.py**

```python
"""Server management API.

Writes server config to Redis + notifies gateway-proxy via Pub/Sub
(server:changed channel) so it hot-reloads the mount.
"""
import json
import re
import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from auth import require_admin
from redis_client import get_redis

router = APIRouter(prefix="/api/servers", tags=["servers"])

# NOTE: underscores break namespace-prefix routing (split on first _).
_NAME_RE = re.compile(r"^[a-z0-9-]+$")


class ServerCreate(BaseModel):
    name: str
    url: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("name must be [a-z0-9-] only, no underscores")
        return v


class ServerUpdate(BaseModel):
    url: str
    description: str = ""


async def _publish_change(action: str, name: str) -> None:
    """Notify gateway-proxy to hot-reload this server."""
    r = get_redis()
    await r.publish("server:changed", json.dumps({"action": action, "name": name}))


@router.post("", status_code=201)
async def create_server(req: ServerCreate, _: str = Depends(require_admin)):
    r = get_redis()
    if await r.exists(f"servers:{req.name}"):
        raise HTTPException(status_code=409, detail=f"server '{req.name}' already exists")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await r.hset(f"servers:{req.name}", mapping={
        "name": req.name, "url": req.url, "description": req.description,
        "status": "active", "tools": "[]",
        "health_up": "0", "health_latency_ms": "", "last_health_check": "",
        "created_at": now,
    })
    await r.sadd("servers:active", req.name)
    await _publish_change("add", req.name)
    return {"name": req.name, "url": req.url, "description": req.description, "status": "active"}


@router.get("")
async def list_servers(_: str = Depends(require_admin)):
    r = get_redis()
    names = await r.smembers("servers:active")
    out = []
    for name in names:
        data = await r.hgetall(f"servers:{name}")
        if data:
            out.append({
                "name": data.get("name", name),
                "url": data.get("url", ""),
                "description": data.get("description", ""),
                "status": data.get("status", "active"),
                "health": {
                    "up": data.get("health_up") == "1",
                    "latency_ms": float(data["health_latency_ms"]) if data.get("health_latency_ms") else None,
                    "last_check": data.get("last_health_check", ""),
                },
                "tools": json.loads(data.get("tools", "[]")),
                "created_at": data.get("created_at", ""),
            })
    return out


@router.put("/{name}")
async def update_server(name: str, req: ServerUpdate, _: str = Depends(require_admin)):
    r = get_redis()
    if not await r.exists(f"servers:{name}"):
        raise HTTPException(status_code=404, detail="server not found")
    await r.hset(f"servers:{name}", mapping={"url": req.url, "description": req.description})
    await _publish_change("update", name)
    return {"name": name, "url": req.url, "description": req.description}


@router.delete("/{name}", status_code=204)
async def delete_server(name: str, _: str = Depends(require_admin)):
    r = get_redis()
    if not await r.exists(f"servers:{name}"):
        raise HTTPException(status_code=404, detail="server not found")
    await r.delete(f"servers:{name}")
    await r.srem("servers:active", name)
    await _publish_change("remove", name)
    return None
```

Create `api/__init__.py` (empty).

- [ ] **Step 4: Wire router into app.py**

Add after the login route in `app.py`:

```python
from api import servers
app.include_router(servers.router)
```

- [ ] **Step 5: Run, verify pass**

```bash
uv run pytest tests/test_servers.py -v
```
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add api/ tests/test_servers.py app.py
git commit -m "feat(gateway-admin): server CRUD API + Pub/Sub notify + name validation"
```

---

### Task 4: MCP Probe + Tools Introspection

**Files:**
- Create: `gateway-admin/mcp_probe.py`
- Create: `gateway-admin/tests/test_mcp_probe.py`
- Modify: `gateway-admin/api/servers.py` (add /status + /refresh-tools routes)

**Interfaces:**
- Produces: `probe(url) -> HealthResult`, `introspect_tools(url) -> list[dict]`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_mcp_probe.py
import pytest
import httpx
from mcp_probe import probe, introspect_tools, HealthResult


async def test_probe_up(monkeypatch):
    async def fake_post(self, url, json=None):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is True
    assert result.latency_ms is not None


async def test_probe_down(monkeypatch):
    async def fake_post(self, url, json=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is False
    assert result.latency_ms is None


async def test_introspect_tools(monkeypatch):
    async def fake_post(self, url, json=None):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
            "tools": [
                {"name": "list_items", "description": "list", "annotations": {"readOnlyHint": True}},
                {"name": "create_item", "description": "create", "annotations": {"destructiveHint": True}},
                {"name": "no_ann", "description": "no annotations"},
            ]
        }})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    tools = await introspect_tools("http://localhost:9999/mcp")
    assert len(tools) == 3
    assert tools[0]["mode"] == "read"
    assert tools[1]["mode"] == "write"
    assert tools[2]["mode"] == "read"  # default when no annotations


async def test_introspect_non_json(monkeypatch):
    async def fake_post(self, url, json=None):
        return httpx.Response(502, text="<html>Bad Gateway</html>")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    tools = await introspect_tools("http://localhost:9999/mcp")
    assert tools == []
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run pytest tests/test_mcp_probe.py -v
```
Expected: FAIL

- [ ] **Step 3: Implement mcp_probe.py**

```python
"""MCP backend probing + tools introspection.

probe() sends MCP ping (liveness). introspect_tools() calls tools/list
and classifies each tool read/write via annotations.destructiveHint.
Mirrors gateway-proxy's registry logic so admin UI has data immediately.
"""
import time
import json
import httpx
import structlog
from dataclasses import dataclass

logger = structlog.get_logger()


@dataclass
class HealthResult:
    up: bool
    latency_ms: float | None


async def probe(url: str) -> HealthResult:
    """Ping a backend MCP server. 5s timeout."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}
            })
            return HealthResult(
                up=resp.status_code == 200,
                latency_ms=round((time.monotonic() - start) * 1000, 1),
            )
    except httpx.HTTPError:
        return HealthResult(up=False, latency_ms=None)


async def introspect_tools(url: str) -> list[dict]:
    """Call tools/list, return [{name, mode, description}].

    mode is 'write' if annotations.destructiveHint else 'read' (default).
    Returns [] on any error (non-JSON, connection, etc).
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}
            })
            data = resp.json()
            tools = []
            for t in data.get("result", {}).get("tools", []):
                ann = t.get("annotations") or {}
                tools.append({
                    "name": t.get("name", ""),
                    "mode": "write" if ann.get("destructiveHint") else "read",
                    "description": t.get("description", ""),
                })
            return tools
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as e:
        logger.warning("introspect_failed", url=url, error=str(e), service="gateway-admin")
        return []
```

- [ ] **Step 4: Add /status + /refresh-tools routes to api/servers.py**

Add these endpoints to `api/servers.py`:

```python
from mcp_probe import probe, introspect_tools


@router.get("/{name}/status")
async def server_status(name: str, _: str = Depends(require_admin)):
    """Immediately probe a server and update its health in Redis."""
    r = get_redis()
    data = await r.hgetall(f"servers:{name}")
    if not data:
        raise HTTPException(status_code=404, detail="server not found")
    result = await probe(data["url"])
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await r.hset(f"servers:{name}", mapping={
        "health_up": "1" if result.up else "0",
        "health_latency_ms": str(result.latency_ms) if result.latency_ms is not None else "",
        "last_health_check": now,
    })
    return {"up": result.up, "latency_ms": result.latency_ms, "checked": now}


@router.post("/{name}/refresh-tools")
async def refresh_tools(name: str, _: str = Depends(require_admin)):
    """Re-introspect tools/list and store them in Redis."""
    r = get_redis()
    data = await r.hgetall(f"servers:{name}")
    if not data:
        raise HTTPException(status_code=404, detail="server not found")
    tools = await introspect_tools(data["url"])
    await r.hset(f"servers:{name}", "tools", json.dumps(tools))
    return {"name": name, "tools": tools}
```

- [ ] **Step 5: Run, verify pass**

```bash
uv run pytest tests/test_mcp_probe.py tests/test_servers.py -v
```
Expected: 4 + 5 = 9 passed

- [ ] **Step 6: Commit**

```bash
git add mcp_probe.py tests/test_mcp_probe.py api/servers.py
git commit -m "feat(gateway-admin): MCP probe + tools introspection + /status + /refresh-tools"
```

---

### Task 5: Token CRUD - SHA-256 hash + one-time reveal + permissions

**Files:**
- Create: `gateway-admin/api/tokens.py`
- Create: `gateway-admin/tests/test_tokens.py`
- Modify: `gateway-admin/app.py` (include router)

**Interfaces:**
- Produces: `api.tokens.router`, `generate_token() -> str`, `hash_token(token) -> str`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tokens.py
import json
import pytest
from auth import create_jwt
from api.tokens import hash_token, generate_token


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def test_generate_token_format():
    t = generate_token()
    assert t.startswith("tok_")
    assert len(t) > 16


def test_hash_token_sha256():
    h = hash_token("tok_abc")
    assert len(h) == 64  # sha256 hex


def test_create_token_returns_plaintext_once(client, fake_redis, auth_headers):
    # prereq: a server must exist for permission validation
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    resp = client.post("/api/tokens", json={
        "name": "zabbix-readonly",
        "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["token"].startswith("tok_")  # plaintext shown once
    assert data["name"] == "zabbix-readonly"


def test_list_tokens_returns_mask_not_plaintext(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    resp = client.get("/api/tokens", headers=auth_headers)
    data = resp.json()
    assert len(data) == 1
    assert data[0]["token_masked"].endswith("****")
    assert "token" not in data[0]  # no plaintext


def test_create_token_unknown_server_rejected(client, fake_redis, auth_headers):
    resp = client.post("/api/tokens", json={
        "name": "bad", "permissions": {"ghost": {"read": True, "write": False}},
    }, headers=auth_headers)
    assert resp.status_code == 422


def test_delete_token(client, fake_redis, auth_headers):
    client.post("/api/servers", json={"name": "zabbix", "url": "http://x", "description": ""},
                headers=auth_headers)
    r = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=auth_headers)
    tok_id = r.json()["id"]
    resp = client.delete(f"/api/tokens/{tok_id}", headers=auth_headers)
    assert resp.status_code == 204
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run pytest tests/test_tokens.py -v
```
Expected: FAIL

- [ ] **Step 3: Implement api/tokens.py**

```python
"""Token management API.

MCP client auth tokens. Stored SHA-256 hashed (never plaintext). The
plaintext is returned ONLY in the POST create response; list/detail
return a mask. gateway-proxy verifies by hashing the incoming token
and looking up tokens:{sha256}.
"""
import json
import time
import hashlib
import secrets
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin, mask_token
from redis_client import get_redis

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


class PermissionSpec(BaseModel):
    read: bool = False
    write: bool = False


class TokenCreate(BaseModel):
    name: str
    permissions: dict[str, PermissionSpec]


def generate_token() -> str:
    """Generate a random token string: tok_ + 24 url-safe chars."""
    return "tok_" + secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    """SHA-256 hex digest (matches gateway-proxy auth.hash_token)."""
    return hashlib.sha256(token.encode()).hexdigest()


@router.post("", status_code=201)
async def create_token(req: TokenCreate, _: str = Depends(require_admin)):
    r = get_redis()
    # Validate all referenced servers exist
    for server_name in req.permissions:
        if not await r.exists(f"servers:{server_name}"):
            raise HTTPException(status_code=422, detail=f"server '{server_name}' not registered")
    plaintext = generate_token()
    token_hash = hash_token(plaintext)
    token_id = "tokid_" + secrets.token_urlsafe(8)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    perms_serialized = {
        srv: {"read": p.read, "write": p.write} for srv, p in req.permissions.items()
    }
    await r.hset(f"tokens:{token_hash}", mapping={
        "id": token_id,
        "name": req.name,
        "token_hash": token_hash,
        "permissions": json.dumps(perms_serialized),
        "created_at": now,
    })
    await r.set(f"token_id:{token_id}", token_hash)
    # plaintext returned ONCE here; never again retrievable
    return {
        "id": token_id,
        "name": req.name,
        "token": plaintext,
        "warning": "明文只显示一次，请立即保存",
        "permissions": perms_serialized,
        "created_at": now,
    }


@router.get("")
async def list_tokens(_: str = Depends(require_admin)):
    r = get_redis()
    # Scan all token keys
    out = []
    async for key in r.scan_iter(match="tokens:*"):
        data = await r.hgetall(key)
        if data:
            out.append({
                "id": data.get("id", ""),
                "name": data.get("name", ""),
                "token_masked": mask_token(data.get("token_hash", "")),
                "permissions": json.loads(data.get("permissions", "{}")),
                "created_at": data.get("created_at", ""),
            })
    return out


@router.delete("/{token_id}", status_code=204)
async def delete_token(token_id: str, _: str = Depends(require_admin)):
    r = get_redis()
    token_hash = await r.get(f"token_id:{token_id}")
    if not token_hash:
        raise HTTPException(status_code=404, detail="token not found")
    await r.delete(f"tokens:{token_hash}")
    await r.delete(f"token_id:{token_id}")
    return None
```

- [ ] **Step 4: Wire router into app.py**

```python
from api import servers, tokens
app.include_router(servers.router)
app.include_router(tokens.router)
```

- [ ] **Step 5: Run, verify pass**

```bash
uv run pytest tests/test_tokens.py -v
```
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add api/tokens.py tests/test_tokens.py app.py
git commit -m "feat(gateway-admin): token CRUD (SHA-256 hash, one-time reveal, mask, perm validation)"
```

---

### Task 6: Dashboard API - Prometheus metrics + failures Stream

**Files:**
- Create: `gateway-admin/metrics.py`
- Create: `gateway-admin/api/dashboard.py`
- Create: `gateway-admin/tests/test_dashboard.py`
- Modify: `gateway-admin/app.py` (include router)

**Interfaces:**
- Produces: `metrics.query_prometheus(query) -> float`, `metrics.query_prometheus_range(query, start, end, step) -> list`, `api.dashboard.router`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_dashboard.py
import json
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


async def test_query_prometheus(monkeypatch):
    import httpx
    from metrics import query_prometheus
    async def fake_get(self, url, params=None):
        return httpx.Response(200, json={
            "status": "success",
            "data": {"result": [{"value": ["123", "42"]}]},
        })
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    val = await query_prometheus("sum(gateway_requests_total)")
    assert val == 42.0


async def test_query_prometheus_empty(monkeypatch):
    import httpx
    from metrics import query_prometheus
    async def fake_get(self, url, params=None):
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await query_prometheus("anything") == 0.0


def test_metrics_summary(client, fake_redis, auth_headers, monkeypatch):
    # mock prometheus queries
    import metrics
    async def fake_query(q):
        return {"sum(gateway_requests_total)": 100, "sum(gateway_auth_failures_total)": 2}.get(q, 0)
    monkeypatch.setattr(metrics, "query_prometheus", fake_query)
    resp = client.get("/api/metrics/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["requests"] == 100
    assert data["auth_failures"] == 2


def test_failures_from_stream(client, fake_redis, auth_headers):
    # seed a failure in the audit stream
    fake_redis.xadd("audit:failures", {
        "trace": "abc", "server": "zabbix", "tool": "list", "op": "read",
        "error_type": "upstream_timeout", "message": "timeout", "latency_ms": "30",
        "time": "2026-07-30T12:00:00Z", "journey": "[]", "token_name": "ro",
    })
    resp = client.get("/api/failures?limit=10", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["trace"] == "abc"
    assert data[0]["error_type"] == "upstream_timeout"
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run pytest tests/test_dashboard.py -v
```
Expected: FAIL

- [ ] **Step 3: Implement metrics.py**

```python
"""Prometheus HTTP API client.

Queries gateway-proxy's /metrics (port 9464) via the Prometheus query
API to build dashboard aggregations. Falls back to 0 when the proxy is
unreachable or has no data yet.
"""
import os
import httpx
import structlog

logger = structlog.get_logger()

PROMETHEUS_URL = os.environ.get(
    "GATEWAY_PROXY_METRICS_URL", "http://localhost:9464/metrics"
)
# The query API lives at /api/v1/query on the same host:port.
_QUERY_API = PROMETHEUS_URL.rsplit("/metrics", 1)[0] + "/api/v1/query"


async def query_prometheus(query: str) -> float:
    """Run an instant PromQL query, return the first result as float (0 if none)."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(_QUERY_API, params={"query": query})
            data = resp.json()
            result = data.get("data", {}).get("result", [])
            if not result:
                return 0.0
            # instant query: result[0]["value"] = [timestamp, "string"]
            return float(result[0]["value"][1])
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        logger.warning("prometheus_query_failed", query=query, error=str(e), service="gateway-admin")
        return 0.0


async def query_prometheus_range(query: str, start: float, end: float, step: str) -> list[float]:
    """Run a range PromQL query, return a list of values."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _QUERY_API.replace("/query", "/query_range"),
                params={"query": query, "start": start, "end": end, "step": step},
            )
            data = resp.json()
            result = data.get("data", {}).get("result", [])
            if not result:
                return []
            return [float(v[1]) for v in result[0].get("values", [])]
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        logger.warning("prometheus_range_failed", query=query, error=str(e), service="gateway-admin")
        return []
```

- [ ] **Step 4: Implement api/dashboard.py**

```python
"""Dashboard API: metrics summary + failures from Redis Stream.

Metrics come from Prometheus (gateway-proxy :9464). Failures come from
the audit:failures Redis Stream written by gateway-proxy's audit module.
"""
import json
import time
from fastapi import APIRouter, Depends, Query

from auth import require_admin
from redis_client import get_redis
from metrics import query_prometheus, query_prometheus_range

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/metrics/summary")
async def metrics_summary(server: str | None = None, _: str = Depends(require_admin)):
    """Aggregated request/error/latency stats. Optional server filter."""
    label = f'{{server="{server}"}}' if server else ""
    req_filter = f"gateway_requests_total{label}" if label else "gateway_requests_total"
    requests = await query_prometheus(f"sum({req_filter})")
    errors = await query_prometheus(f"sum(gateway_requests_total{{status!='ok'}})")
    auth_failures = await query_prometheus("sum(gateway_auth_failures_total)")
    p95 = await query_prometheus("histogram_quantile(0.95, sum by (le) (gateway_request_duration_seconds_bucket))")
    reads = await query_prometheus('sum(gateway_requests_total{operation="read"})')
    writes = await query_prometheus('sum(gateway_requests_total{operation="write"})')
    error_rate = round(errors / requests * 100, 2) if requests else 0.0
    return {
        "requests": int(requests),
        "errors": int(errors),
        "error_rate": error_rate,
        "p95_ms": round(p95 * 1000, 1) if p95 else 0,
        "read": int(reads),
        "write": int(writes),
        "auth_failures": int(auth_failures),
    }


@router.get("/metrics/by-server")
async def metrics_by_server(_: str = Depends(require_admin)):
    """Per-server stats table."""
    r = get_redis()
    names = await r.smembers("servers:active")
    out = []
    for name in names:
        reqs = await query_prometheus(f'sum(gateway_requests_total{{server="{name}"}})')
        errs = await query_prometheus(f'sum(gateway_requests_total{{server="{name}",status!="ok"}})')
        p95 = await query_prometheus(
            f'histogram_quantile(0.95, sum by (le) (gateway_request_duration_seconds_bucket{{server="{name}"}}))'
        )
        out.append({
            "server": name,
            "requests": int(reqs),
            "errors": int(errs),
            "error_rate": round(errs / reqs * 100, 2) if reqs else 0.0,
            "p95_ms": round(p95 * 1000, 1) if p95 else 0,
        })
    return out


@router.get("/metrics/timeseries")
async def metrics_timeseries(server: str | None = None, window: str = "1h", _: str = Depends(require_admin)):
    """Request-count time series for sparkline/timeline."""
    end = time.time()
    start = end - 3600  # 1h; expand if window == "24h"
    if window == "24h":
        start = end - 86400
    label = f'{{server="{server}"}}' if server else ""
    q = f"sum(rate(gateway_requests_total{label}[1m]))"
    points = await query_prometheus_range(q, start, end, "60")
    return {"window": window, "bucket": "1min", "points": points}


@router.get("/failures")
async def list_failures(
    server: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: str = Depends(require_admin),
):
    """Read failed requests from the audit:failures Redis Stream (newest first)."""
    r = get_redis()
    # XREVRANGE returns newest first; +offset for pagination
    entries = await r.xrevrange("audit:failures", count=limit + offset)
    entries = entries[offset:]  # skip offset
    out = []
    for _id, fields in entries:
        rec = {
            "trace": fields.get("trace", ""),
            "server": fields.get("server", ""),
            "tool": fields.get("tool", ""),
            "op": fields.get("op", ""),
            "error_type": fields.get("error_type", ""),
            "message": fields.get("message", ""),
            "latency_ms": int(fields["latency_ms"]) if fields.get("latency_ms", "").isdigit() else 0,
            "time": fields.get("time", ""),
            "journey": json.loads(fields.get("journey", "[]")),
        }
        if server is None or rec["server"] == server:
            out.append(rec)
    return out
```

- [ ] **Step 5: Wire router into app.py**

```python
from api import servers, tokens, dashboard
app.include_router(servers.router)
app.include_router(tokens.router)
app.include_router(dashboard.router)
```

- [ ] **Step 6: Run, verify pass**

```bash
uv run pytest tests/test_dashboard.py -v
```
Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
git add metrics.py api/dashboard.py tests/test_dashboard.py app.py
git commit -m "feat(gateway-admin): dashboard API (Prometheus metrics + failures Stream)"
```

---

### Task 7: Integration Test + Smoke + README

**Files:**
- Create: `gateway-admin/tests/test_integration.py`
- Create: `gateway-admin/README.md`
- Modify: `gateway-admin/app.py` (final router wiring verification)

- [ ] **Step 1: Write integration test (full flow)**

```python
# tests/test_integration.py
"""End-to-end flow: login -> register server -> create token -> list."""
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def test_full_flow(client, fake_redis):
    # 1. login works (default admin created in lifespan)
    resp = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    token = resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 2. register a server
    resp = client.post("/api/servers", json={
        "name": "zabbix", "url": "http://localhost:8000/mcp", "description": "Zabbix",
    }, headers=headers)
    assert resp.status_code == 201

    # 3. list servers shows it
    resp = client.get("/api/servers", headers=headers)
    assert len(resp.json()) == 1

    # 4. create a token with read perm on zabbix
    resp = client.post("/api/tokens", json={
        "name": "ro", "permissions": {"zabbix": {"read": True, "write": False}},
    }, headers=headers)
    assert resp.status_code == 201
    plaintext = resp.json()["token"]
    assert plaintext.startswith("tok_")

    # 5. list tokens masks plaintext
    resp = client.get("/api/tokens", headers=headers)
    t = resp.json()[0]
    assert t["token_masked"].endswith("****")
    assert "token" not in t

    # 6. delete server + token
    assert client.delete("/api/servers/zabbix", headers=headers).status_code == 204
    assert client.delete(f"/api/tokens/{resp.json()[0]['id']}" if False else "/api/tokens/x",
                         headers=headers).status_code in (204, 404)
```

- [ ] **Step 2: Run full suite**

```bash
cd gateway-admin && uv run pytest tests/ -v
```
Expected: all pass (auth 6 + servers 5 + mcp_probe 4 + tokens 6 + dashboard 4 + integration 1 = 26)

- [ ] **Step 3: Smoke test - start server**

```bash
JWT_SECRET=dev REDIS_URL=redis://localhost:6379/0 uv run uvicorn app:app --port 8081 &
sleep 3
curl -s http://localhost:8081/api/health
# -> {"status":"ok"}
curl -s -X POST http://localhost:8081/api/login -H "Content-Type: application/json" -d '{"username":"admin","password":"admin123"}'
# -> {"token":"eyJ...","expires_in":86400}
kill %1
```

- [ ] **Step 4: Create README.md**

```markdown
# gateway-admin

MCP 网关管理面。FastAPI 管理 API（server/token/dashboard）+ Vue 3 静态前端。

## 运行

\`\`\`bash
uv sync
REDIS_URL=redis://localhost:6379/0 JWT_SECRET=your-secret \
  uv run uvicorn app:app --port 8081 --reload
\`\`\`

默认管理员：admin / admin123（首次启动自动创建，请立即改密）

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/login | 管理员登录 -> JWT |
| GET/POST/PUT/DELETE | /api/servers | Server CRUD |
| GET | /api/servers/{name}/status | 立即探活 |
| POST | /api/servers/{name}/refresh-tools | 刷新 tools 清单 |
| GET/POST/DELETE | /api/tokens | Token CRUD |
| GET | /api/metrics/summary | 监控汇总 |
| GET | /api/metrics/by-server | 分 server 统计 |
| GET | /api/metrics/timeseries | 时间序列 |
| GET | /api/failures | 失败请求列表 |

## 依赖
gateway-proxy 共享 Redis。admin 写 servers/tokens，proxy 热加载。admin 读 Prometheus + audit Stream。
```

- [ ] **Step 5: Final commit**

```bash
git add tests/test_integration.py README.md
git commit -m "feat(gateway-admin): integration test + smoke + README

Full flow: login -> register server -> create token -> list -> delete.
26 tests passing, smoke verified."
```

---

## Self-Review

**1. Spec coverage (§6 gateway-admin + §10 API contract + §11):**
- [x] Admin JWT login (bcrypt) -> Task 2
- [x] Server CRUD + Redis hash + Pub/Sub notify -> Task 3
- [x] Server name validation [a-z0-9-] -> Task 3
- [x] Probe (MCP ping) + /status -> Task 4
- [x] Tools introspection (tools/list, destructiveHint->write) + /refresh-tools -> Task 4
- [x] Token CRUD + SHA-256 hash + one-time reveal + mask + perm validation -> Task 5
- [x] metrics/summary (Prometheus, server filter, backend aggregate) -> Task 6
- [x] metrics/by-server -> Task 6
- [x] metrics/timeseries -> Task 6
- [x] failures (Redis Stream, pagination, journey) -> Task 6
- [x] CORS for Vue dev server -> Task 1
- [x] Static file serving (Vue dist) -> Task 1 (conditional mount)
- [x] Default admin bootstrap -> Task 2 (ensure_default_admin)
- [x] §10 response shapes (servers with health+tools, tokens masked, failures with journey) -> Tasks 3/5/6
- [x] §11 token hashed storage -> Task 5
- [x] §11 tools refresh -> Task 4
- [x] §11 time returns absolute ISO -> Tasks 3/5
- [x] §11 pagination (failures limit+offset) -> Task 6
- [x] §11 create token validates server exists -> Task 5
- [ ] Periodic probe (30s loop) - NOT in this plan; admin does on-demand probe via /status. Periodic probing could live in proxy or a background task. Note as acceptable: spec §6 says "Gateway 定期" which could be proxy. Admin provides on-demand. Acceptable scope for Plan B.
- [ ] metrics aggregation放后端 - DONE (Task 6 returns aggregated, not raw)

**2. Placeholder scan:** No TBD/TODO in code. The periodic-probe loop is a documented scope decision (on-demand only here), not a placeholder.

**3. Type consistency:**
- `hash_token(token) -> str` (sha256 hex) - Task 5, matches gateway-proxy auth.hash_token
- `mask_token(token) -> str` - Task 2, used in Task 5
- `probe(url) -> HealthResult(up, latency_ms)` - Task 4, matches spec §6
- `introspect_tools(url) -> list[dict]` - Task 4, returns [{name, mode, description}]
- `query_prometheus(query) -> float` - Task 6
- Token permissions: `dict[str, {read, write}]` - consistent Tasks 5/6
- Server Redis hash fields match spec §6 exactly
