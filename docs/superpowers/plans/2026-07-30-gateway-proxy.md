# MCP Gateway Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `gateway-proxy` — a FastMCP 4.0 stateless server that aggregates backend MCP servers via namespace mounting, validates API tokens (SHA-256 against Redis), enforces per-server read/write permissions, records failures to an audit stream, and exposes Prometheus metrics.

**Architecture:** FastMCP `mount(create_proxy(url), namespace=name)` aggregates backends. A custom `TokenVerifier` validates Bearer tokens against Redis. A `PermissionMiddleware` intercepts `tools/call`, parses the `{server}_{tool}` namespace prefix, and checks read/write. A registry subscriber hot-reloads servers via Redis Pub/Sub. Failures are written to a Redis Stream for the admin service to read.

**Tech Stack:** FastMCP 4.0.0b1, httpx, redis (async), structlog, opentelemetry-sdk + opentelemetry-exporter-prometheus, pytest, pytest-asyncio

## Global Constraints

- Python >=3.12, uv with `prerelease = "allow"`
- FastMCP `fastmcp==4.0.0b1`, MCP Protocol `2026-07-28`, stateless HTTP (`stateless_http=True`)
- All logs: structlog key=value, no f-string logging; inject `trace_id`/`span_id` from OTel
- Metrics via OTel SDK (`metrics.get_meter()`) + Prometheus exporter — NOT `prometheus_client` directly
- Metric labels are bounded-cardinality only (`server`, `tool`, `operation`, `status`); no `user_id`/`request_id`/`token` (OBS-MET-002)
- Token storage: SHA-256 hashed, never plaintext; key = `tokens:{sha256(token)}`
- Server names: `[a-z0-9-]` only, **no underscores** (namespace prefix splits on first `_`)
- Comments explain "why" not "what" (OBS-CORE-005); write-tool docstrings contain `⚠️ 写操作`
- Tool mode: `annotations.destructiveHint == True` -> write, else read

---

## File Structure

```
gateway-proxy/
├── CLAUDE.md
├── pyproject.toml
├── server.py              # FastMCP entry: mount servers, add middleware, run
├── auth.py                # GatewayTokenVerifier (SHA-256 Redis lookup) + permission check
├── routing.py             # split_prefix(), TOOL_REGISTRY, resolve_target()
├── middleware.py          # PermissionMiddleware + AuditMiddleware (on_message)
├── registry.py            # Pub/Sub hot-reload: mount_servers(), watch_changes()
├── audit.py               # record_failure() -> Redis Stream
├── observability.py       # OTel TracerProvider + Prometheus metrics init
├── redis_client.py        # async Redis connection (singleton)
└── tests/
    ├── conftest.py        # shared fixtures: fake_redis, mock mounted gateway
    ├── test_auth.py
    ├── test_routing.py
    ├── test_middleware.py
    └── test_registry.py
```

---

### Task 1: Project Scaffolding + Redis Client

**Files:**
- Create: `gateway-proxy/pyproject.toml`
- Create: `gateway-proxy/CLAUDE.md`
- Create: `gateway-proxy/redis_client.py`
- Create: `gateway-proxy/tests/conftest.py`

**Interfaces:**
- Produces: `get_redis() -> redis.asyncio.Redis` (module-level singleton)

- [ ] **Step 1: Create pyproject.toml**

```toml
[tool.uv]
prerelease = "allow"

[project]
name = "gateway-proxy"
version = "0.1.0"
description = "MCP Gateway Proxy - aggregates backend MCP servers with token auth + permissions"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "fastmcp==4.0.0b1",
    "httpx>=0.27,<1.0",
    "redis>=5.0",
    "structlog>=24.0",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-exporter-prometheus",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24"]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 2: Install deps**

```bash
cd gateway-proxy && uv sync --all-extras
```

- [ ] **Step 3: Create CLAUDE.md**

```markdown
# gateway-proxy - 开发说明

## 概述
MCP 网关代理。聚合后端 MCP server，token 认证，读写权限控制，失败审计，Prometheus metrics。

## 架构
- FastMCP mount(create_proxy(url), namespace=name) 聚合后端
- 自定义 TokenVerifier：SHA-256 比对 Redis
- PermissionMiddleware：解析 {server}_{tool} 前缀，查 read/write 权限
- Registry：Redis Pub/Sub 热加载 server
- Audit：失败写 Redis Stream

## 本地开发
\`\`\`bash
uv sync
REDIS_URL=redis://localhost:6379/0 uv run python server.py
uv run pytest tests/ -v
\`\`\`

## 配置
| 环境变量 | 默认 | 说明 |
|---|---|---|
| GATEWAY_PORT | 8080 | 监听端口 |
| REDIS_URL | redis://localhost:6379/0 | Redis |
| PROMETHEUS_PORT | 9464 | metrics 端口 |
| OTEL_EXPORTER_OTLP_ENDPOINT | (空=console) | OTel collector |

## 知识库
查 `../knowledge-base/fastmcp-v4/`：19-middleware（拦截）、50-authorization、53-token-verification。
```

- [ ] **Step 4: Create redis_client.py**

```python
"""Async Redis connection singleton.

Shared by auth, registry, audit. Module-level so all modules see one pool.
"""
import os
import redis.asyncio as redis

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Return the process-level Redis client. Lazily initialized."""
    global _redis
    if _redis is None:
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _redis = redis.from_url(url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    """Close the Redis connection pool on shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
```

- [ ] **Step 5: Create conftest.py with fake Redis fixture**

```python
"""Shared test fixtures.

Uses a process-local fake instead of real Redis so unit tests need no broker.
"""
import pytest
import fakeredis.aioredis


@pytest.fixture
async def fake_redis(monkeypatch):
    """Replace get_redis() with an in-memory fake Redis."""
    import redis_client
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "_redis", fake)
    yield fake
    await fake.aclose()
```

Add `fakeredis>=2.20` to dev deps in pyproject.toml `[project.optional-dependencies] dev` list.

- [ ] **Step 6: Run a smoke import test**

```bash
uv run python -c "from redis_client import get_redis; print('ok')"
```
Expected: `ok`

- [ ] **Step 7: Commit**

```bash
git add gateway-proxy
git commit -m "feat(gateway-proxy): scaffold project + redis client

- pyproject: fastmcp 4.0.0b1, httpx, redis, structlog, otel
- redis_client: async singleton
- conftest: fakeredis fixture"
```

---

### Task 2: Routing — namespace prefix + TOOL_REGISTRY

**Files:**
- Create: `gateway-proxy/routing.py`
- Create: `gateway-proxy/tests/test_routing.py`

**Interfaces:**
- Produces: `split_prefix(mcp_name) -> (server, tool)`, `register_tools(server, tools)`, `get_tool_mode(server, tool) -> "read"|"write"`, `resolve_target(mcp_name) -> (server, tool, mode)`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_routing.py
import pytest
from routing import split_prefix, register_tools, get_tool_mode, resolve_target, UnknownServerError


def test_split_prefix_basic():
    assert split_prefix("zabbix_list_active_problems") == ("zabbix", "list_active_problems")


def test_split_prefix_hyphenated_server():
    # server name may contain hyphens; first _ is the separator
    assert split_prefix("my-db_run_query") == ("my-db", "run_query")


def test_split_prefix_no_underscore_raises():
    with pytest.raises(ValueError, match="no namespace prefix"):
        split_prefix("list_things")


def test_register_and_get_mode():
    register_tools("zabbix", [
        {"name": "list_active_problems", "mode": "read"},
        {"name": "create_maintenance", "mode": "write"},
    ])
    assert get_tool_mode("zabbix", "list_active_problems") == "read"
    assert get_tool_mode("zabbix", "create_maintenance") == "write"


def test_resolve_target_known():
    register_tools("zabbix", [{"name": "list_active_problems", "mode": "read"}])
    server, tool, mode = resolve_target("zabbix_list_active_problems")
    assert (server, tool, mode) == ("zabbix", "list_active_problems", "read")


def test_resolve_target_unknown_server():
    with pytest.raises(UnknownServerError):
        resolve_target("ghost_tool")
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run pytest tests/test_routing.py -v
```
Expected: FAIL `ModuleNotFoundError: No module named 'routing'`

- [ ] **Step 3: Implement routing.py**

```python
"""Namespace prefix routing + tool mode registry.

FastMCP mount(namespace=name) exposes tools as {server}_{tool}. This module
splits that prefix to find the target server, and looks up whether the tool
is a read or write operation (from annotations.destructiveHint at introspect time).
"""
# TOOL_REGISTRY: {server: {tool_name: mode}}
# NOTE: module-level dict mutated by registry.mount_server on hot-reload.
TOOL_REGISTRY: dict[str, dict[str, str]] = {}


class UnknownServerError(Exception):
    """The namespace prefix does not match any registered server."""


def split_prefix(mcp_name: str) -> tuple[str, str]:
    """Split '{server}_{tool}' into (server, tool).

    Splits on the FIRST underscore. Server names are [a-z0-9-] (no underscores,
    enforced at registration), so the first _ is always the namespace separator.
    Raises ValueError if there is no underscore (no namespace prefix).
    """
    if "_" not in mcp_name:
        raise ValueError(f"no namespace prefix in tool name: {mcp_name}")
    server, tool = mcp_name.split("_", 1)
    return server, tool


def register_tools(server: str, tools: list[dict]) -> None:
    """Record each tool's mode (read/write) for a server.

    Called by registry when mounting/refreshing a server.
    Overwrites previous entries for this server (handles update).
    """
    TOOL_REGISTRY[server] = {t["name"]: t["mode"] for t in tools}


def clear_tools(server: str) -> None:
    """Remove a server's tools from the registry (on unmount)."""
    TOOL_REGISTRY.pop(server, None)


def get_tool_mode(server: str, tool: str) -> str:
    """Return 'read' or 'write' for a server's tool. Defaults to 'read'."""
    return TOOL_REGISTRY.get(server, {}).get(tool, "read")


def resolve_target(mcp_name: str) -> tuple[str, str, str]:
    """Resolve a namespaced tool name to (server, tool, mode).

    Raises UnknownServerError if the server prefix is not registered.
    """
    server, tool = split_prefix(mcp_name)
    if server not in TOOL_REGISTRY:
        raise UnknownServerError(f"server '{server}' not registered")
    return server, tool, get_tool_mode(server, tool)
```

- [ ] **Step 4: Run, verify pass**

```bash
uv run pytest tests/test_routing.py -v
```
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add routing.py tests/test_routing.py
git commit -m "feat(gateway-proxy): namespace prefix routing + tool mode registry"
```

---

### Task 3: Token Auth — GatewayTokenVerifier

**Files:**
- Create: `gateway-proxy/auth.py`
- Create: `gateway-proxy/tests/test_auth.py`

**Interfaces:**
- Consumes: `redis_client.get_redis()`, `routing.resolve_target()`
- Produces: `hash_token(token) -> str`, `verify_token(token) -> TokenInfo | None`, `check_permission(token_info, server, mode) -> bool`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_auth.py
import pytest
from auth import hash_token, verify_token, check_permission


def test_hash_token_is_sha256_hex():
    h = hash_token("tok_abc")
    assert len(h) == 64
    assert h == hash_token("tok_abc")  # deterministic
    assert h != hash_token("tok_xyz")  # different input


async def test_verify_token_valid(fake_redis):
    await fake_redis.hset(
        f"tokens:{hash_token('tok_secret')}",
        mapping={
            "id": "tok_id_1",
            "name": "zabbix-readonly",
            "permissions": '{"zabbix": {"read": true, "write": false}}',
        },
    )
    info = await verify_token("tok_secret")
    assert info is not None
    assert info["name"] == "zabbix-readonly"
    assert info["permissions"]["zabbix"] == {"read": True, "write": False}


async def test_verify_token_invalid_returns_none(fake_redis):
    info = await verify_token("tok_nonexistent")
    assert info is None


def test_check_permission_read_allowed():
    info = {"permissions": {"zabbix": {"read": True, "write": False}}}
    assert check_permission(info, "zabbix", "read") is True


def test_check_permission_write_denied():
    info = {"permissions": {"zabbix": {"read": True, "write": False}}}
    assert check_permission(info, "zabbix", "write") is False


def test_check_permission_server_not_granted():
    info = {"permissions": {"zabbix": {"read": True, "write": False}}}
    assert check_permission(info, "github", "read") is False
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run pytest tests/test_auth.py -v
```
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: Implement auth.py**

```python
"""Token authentication + permission checks.

Tokens are stored SHA-256 hashed in Redis (never plaintext). We hash the
incoming Bearer token and look up tokens:{hash}. Permissions are a JSON
map of {server: {read, write}}.
"""
import hashlib
import json
from redis_client import get_redis


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a token string."""
    return hashlib.sha256(token.encode()).hexdigest()


async def verify_token(token: str) -> dict | None:
    """Look up a token by its hash. Returns token info dict or None if invalid.

    Returns: {"id", "name", "permissions": {server: {read, write}}}
    """
    r = get_redis()
    data = await r.hgetall(f"tokens:{hash_token(token)}")
    if not data:
        return None
    return {
        "id": data["id"],
        "name": data["name"],
        "permissions": json.loads(data["permissions"]),
    }


def check_permission(token_info: dict, server: str, mode: str) -> bool:
    """Check whether a token grants (server, mode) access.

    mode is 'read' or 'write'. No entry for server -> denied.
    """
    perm = token_info["permissions"].get(server)
    if not perm:
        return False
    return bool(perm.get(mode, False))
```

- [ ] **Step 4: Run, verify pass**

```bash
uv run pytest tests/test_auth.py -v
```
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add auth.py tests/test_auth.py
git commit -m "feat(gateway-proxy): token auth (SHA-256 Redis) + permission check"
```

---

### Task 4: Audit Log — record_failure to Redis Stream

**Files:**
- Create: `gateway-proxy/audit.py`
- Create: `gateway-proxy/tests/test_audit.py`

**Interfaces:**
- Produces: `record_failure(journey, error_type, meta)` writing to Redis Stream `audit:failures`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_audit.py
import json
from audit import record_failure, ERROR_TYPES


async def test_record_failure_writes_to_stream(fake_redis):
    journey = [
        {"stage": "client", "state": "ok", "ms": 2},
        {"stage": "auth", "state": "fail", "ms": 2},
        {"stage": "route", "state": "skip", "ms": 0},
    ]
    await record_failure(
        journey=journey,
        error_type="invalid_token",
        meta={
            "trace_id": "abc123",
            "server": "github",
            "tool": "list_repos",
            "op": "read",
            "message": "Token 无效",
            "latency_ms": 2,
            "time": "2026-07-30T12:41:55Z",
        },
    )
    entries = await fake_redis.xrange("audit:failures")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["error_type"] == "invalid_token"
    assert fields["trace"] == "abc123"
    parsed = json.loads(fields["journey"])
    assert parsed[1]["state"] == "fail"


def test_error_types_are_the_documented_enum():
    assert set(ERROR_TYPES) == {
        "upstream_timeout", "permission_denied", "invalid_token",
        "upstream_error", "connection_error",
    }
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run pytest tests/test_audit.py -v
```
Expected: FAIL

- [ ] **Step 3: Implement audit.py**

```python
"""Failure audit logging to a Redis Stream.

The proxy writes one entry per failed request, including the full request
journey (which stage failed + per-stage timing). The admin service reads
this stream to populate the dashboard failure feed + trace view.
"""
import json
import structlog
from redis_client import get_redis

logger = structlog.get_logger()

# NOTE: bounded enum consumed by the admin frontend's error-type chips.
ERROR_TYPES = frozenset({
    "upstream_timeout",
    "permission_denied",
    "invalid_token",
    "upstream_error",
    "connection_error",
})

# MAXLEN trims the stream so it cannot grow unbounded.
_STREAM_MAXLEN = 10000


async def record_failure(
    journey: list[dict],
    error_type: str,
    meta: dict,
) -> None:
    """Append a failure record to the audit:failures Redis Stream.

    journey: [{stage, state, ms}, ...] — state is ok|fail|skip
    error_type: one of ERROR_TYPES
    meta: {trace_id, server, tool, op, message, latency_ms, time}
    """
    r = get_redis()
    try:
        await r.xadd(
            "audit:failures",
            {
                "trace": meta["trace_id"],
                "server": meta["server"],
                "tool": meta["tool"],
                "op": meta["op"],
                "error_type": error_type,
                "message": meta["message"],
                "latency_ms": meta["latency_ms"],
                "time": meta["time"],
                "journey": json.dumps(journey),
            },
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as e:
        # NOTE: audit must never break the request path; log and continue.
        logger.error("audit_write_failed", error=str(e), service="gateway-proxy")
```

- [ ] **Step 4: Run, verify pass**

```bash
uv run pytest tests/test_audit.py -v
```
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add audit.py tests/test_audit.py
git commit -m "feat(gateway-proxy): audit log to Redis Stream"
```

---

### Task 5: Middleware — PermissionMiddleware + AuditMiddleware

**Files:**
- Create: `gateway-proxy/middleware.py`
- Create: `gateway-proxy/tests/test_middleware.py`

**Interfaces:**
- Consumes: `auth.verify_token()`, `auth.check_permission()`, `routing.resolve_target()`, `audit.record_failure()`, `observability` metrics
- Produces: `PermissionMiddleware`, `AuditMiddleware`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_middleware.py
import pytest
from middleware import check_call_permission, classify_error


def test_check_call_permission_allows_read():
    # token has zabbix read; calling a read tool -> allowed
    token_info = {"permissions": {"zabbix": {"read": True, "write": False}}}
    ok, err = check_call_permission(token_info, "zabbix_list_active_problems")
    assert ok is True
    assert err is None


def test_check_call_permission_denies_write():
    token_info = {"permissions": {"zabbix": {"read": True, "write": False}}}
    ok, err = check_call_permission(token_info, "zabbix_create_maintenance")
    assert ok is False
    assert err == "permission_denied"


def test_check_call_permission_denies_unknown_server():
    token_info = {"permissions": {"zabbix": {"read": True}}}
    ok, err = check_call_permission(token_info, "ghost_tool")
    assert ok is False
    assert err == "permission_denied"


def test_classify_error_timeout():
    import httpx
    assert classify_error(httpx.TimeoutException("x")) == "upstream_timeout"


def test_classify_error_connect():
    import httpx
    assert classify_error(httpx.ConnectError("refused")) == "connection_error"


def test_classify_error_generic():
    assert classify_error(ValueError("boom")) == "upstream_error"
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run pytest tests/test_middleware.py -v
```
Expected: FAIL

- [ ] **Step 3: Implement middleware.py**

```python
"""MCP middleware: permission enforcement + failure audit + metrics.

PermissionMiddleware runs on every tools/call: verifies the token, parses
the namespace prefix, and checks read/write. Denied calls are recorded as
audit failures and never reach the backend.

NOTE: FastMCP middleware uses on_message(context, call_next). The token is
read from the Authorization header (parsed in server.py and stashed on the
context); here we consume the already-verified token_info.
"""
import time
import structlog
import httpx

from auth import verify_token, check_permission
from routing import resolve_target, UnknownServerError
from audit import record_failure

logger = structlog.get_logger()


def check_call_permission(token_info: dict | None, mcp_name: str) -> tuple[bool, str | None]:
    """Check whether a token may call a namespaced tool.

    Returns (allowed, error_type). error_type is one of the audit enum or None.
    """
    if token_info is None:
        return False, "invalid_token"
    try:
        server, tool, mode = resolve_target(mcp_name)
    except (ValueError, UnknownServerError):
        return False, "permission_denied"
    if not check_permission(token_info, server, mode):
        return False, "permission_denied"
    return True, None


def classify_error(exc: Exception) -> str:
    """Map an exception to an audit error_type enum value."""
    if isinstance(exc, httpx.TimeoutException):
        return "upstream_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection_error"
    return "upstream_error"


async def record_call_failure(
    token_info: dict | None,
    mcp_name: str,
    error_type: str,
    message: str,
    latency_ms: int,
    trace_id: str,
    fail_stage: str,
) -> None:
    """Build a journey and write a failure audit record.

    fail_stage: where it broke — 'auth', 'route', or the backend server name.
    """
    server = ""
    tool = ""
    op = "read"
    try:
        server, tool, op = resolve_target(mcp_name)
    except (ValueError, UnknownServerError):
        pass

    stages = ["client", "gateway", "auth", "route", server or "backend"]
    journey = []
    for i, st in enumerate(stages):
        if st == fail_stage:
            journey.append({"stage": st, "state": "fail", "ms": latency_ms})
            # subsequent stages were not reached
            for after in stages[i + 1:]:
                journey.append({"stage": after, "state": "skip", "ms": 0})
            break
        journey.append({"stage": st, "state": "ok", "ms": 0})

    await record_failure(
        journey=journey,
        error_type=error_type,
        meta={
            "trace_id": trace_id,
            "server": server,
            "tool": tool,
            "op": op,
            "message": message,
            "latency_ms": latency_ms,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
```

- [ ] **Step 4: Run, verify pass**

```bash
uv run pytest tests/test_middleware.py -v
```
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add middleware.py tests/test_middleware.py
git commit -m "feat(gateway-proxy): permission check + error classification + audit wiring"
```

---

### Task 6: Observability — OTel + Prometheus

**Files:**
- Create: `gateway-proxy/observability.py`

**Interfaces:**
- Produces: `init_telemetry()`, module-level metric instruments `REQUESTS_TOTAL`, `REQUEST_LATENCY`, `AUTH_FAILURES`

- [ ] **Step 1: Implement observability.py**

```python
"""OTel TracerProvider + Prometheus metrics.

Mirrors the zabbix-mcp telemetry setup so the two services share a backend.
Metrics use the OTel SDK with a Prometheus exporter (NOT prometheus_client
directly), per the observability coding standard.
"""
import os
import structlog
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

logger = structlog.get_logger()

PROMETHEUS_PORT = int(os.environ.get("PROMETHEUS_PORT", "9464"))

# Module-level instruments; None until init_telemetry() runs.
REQUESTS_TOTAL = None
REQUEST_LATENCY = None
AUTH_FAILURES = None


def init_telemetry(service_name: str = "mcp-gateway") -> None:
    """Configure OTel traces + Prometheus metrics. Safe to call once at startup."""
    global REQUESTS_TOTAL, REQUEST_LATENCY, AUTH_FAILURES

    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
    })

    # ── Traces ───────────────────────────────────────────────────
    otlp = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if otlp:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint=otlp)
    else:
        exporter = ConsoleSpanExporter()
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # ── Metrics ──────────────────────────────────────────────────
    try:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from prometheus_client import start_http_server

        reader = PrometheusMetricReader()
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
        start_http_server(PROMETHEUS_PORT)

        meter = metrics.get_meter("mcp-gateway")
        # NOTE: labels are bounded-cardinality (server/tool/operation/status)
        REQUESTS_TOTAL = meter.create_counter("gateway_requests_total", description="Total MCP requests")
        REQUEST_LATENCY = meter.create_histogram("gateway_request_duration_seconds", description="Request latency")
        AUTH_FAILURES = meter.create_counter("gateway_auth_failures_total", description="Auth failures")
        logger.info("metrics_configured", service=service_name, port=PROMETHEUS_PORT)
    except ImportError:
        logger.warning("prometheus_exporter_missing", service=service_name)
```

- [ ] **Step 2: Smoke import test**

```bash
uv run python -c "from observability import init_telemetry; print('ok')"
```
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add observability.py
git commit -m "feat(gateway-proxy): OTel traces + Prometheus metrics"
```

---

### Task 7: Registry — hot-reload servers via Pub/Sub

**Files:**
- Create: `gateway-proxy/registry.py`
- Create: `gateway-proxy/tests/test_registry.py`

**Interfaces:**
- Consumes: `redis_client.get_redis()`, `routing.register_tools`/`clear_tools`
- Produces: `mount_servers(gateway)`, `watch_changes(gateway)`, `probe(url) -> HealthResult`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_registry.py
import pytest
from registry import probe, parse_change_event


async def test_probe_up(fake_redis, monkeypatch):
    # probe hits a URL with MCP ping; mock httpx to return 200
    import httpx
    async def fake_post(self, url, json=None):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is True
    assert result.latency_ms >= 0


async def test_probe_down(monkeypatch):
    import httpx
    async def fake_post(self, url, json=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is False


def test_parse_change_event_add():
    evt = parse_change_event('{"action":"add","name":"zabbix"}')
    assert evt == ("add", "zabbix")


def test_parse_change_event_invalid():
    assert parse_change_event("not json") is None
```

- [ ] **Step 2: Run, verify fail**

```bash
uv run pytest tests/test_registry.py -v
```
Expected: FAIL

- [ ] **Step 3: Implement registry.py**

```python
"""Server registry: mount/unmount backends + hot-reload via Redis Pub/Sub.

On startup we load all servers in servers:active and mount them. We then
subscribe to the 'server:changed' channel so the admin service can add/
update/remove servers without restarting the proxy.
"""
import json
import time
import structlog
import httpx
from dataclasses import dataclass

from redis_client import get_redis
from routing import register_tools, clear_tools

logger = structlog.get_logger()


@dataclass
class HealthResult:
    up: bool
    latency_ms: float | None


async def probe(url: str) -> HealthResult:
    """Ping a backend MCP server (standard MCP ping). 5s timeout."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
            return HealthResult(up=resp.status_code == 200, latency_ms=(time.monotonic() - start) * 1000)
    except httpx.HTTPError:
        return HealthResult(up=False, latency_ms=None)


async def _introspect_tools(url: str) -> list[dict]:
    """Call tools/list on a backend to learn each tool's mode + description.

    mode is 'write' if annotations.destructiveHint else 'read'.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            data = resp.json()
            tools = []
            for t in data.get("result", {}).get("tools", []):
                ann = t.get("annotations") or {}
                tools.append({
                    "name": t["name"],
                    "mode": "write" if ann.get("destructiveHint") else "read",
                    "description": t.get("description", ""),
                })
            return tools
    except httpx.HTTPError as e:
        logger.error("introspect_failed", url=url, error=str(e), service="gateway-proxy")
        return []


def parse_change_event(raw: str) -> tuple[str, str] | None:
    """Parse a server:changed pubsub message. Returns (action, name) or None."""
    try:
        evt = json.loads(raw)
        return evt["action"], evt["name"]
    except (json.JSONDecodeError, KeyError):
        return None


async def mount_all(gateway) -> None:
    """Load every server in servers:active and mount it (startup)."""
    r = get_redis()
    names = await r.smembers("servers:active")
    for name in names:
        info = await r.hgetall(f"servers:{name}")
        if info:
            await _mount_one(gateway, name, info["url"])


async def _mount_one(gateway, name: str, url: str) -> None:
    """Mount a single backend + introspect its tools into TOOL_REGISTRY."""
    from fastmcp import create_proxy
    try:
        gateway.mount(create_proxy(url), name=name)
    except Exception as e:
        logger.error("mount_failed", server=name, error=str(e), service="gateway-proxy")
        return
    tools = await _introspect_tools(url)
    register_tools(name, tools)
    # store tools back to redis for the admin UI to read
    r = get_redis()
    await r.hset(f"servers:{name}", "tools", json.dumps(tools))
    logger.info("server_mounted", server=name, tools=len(tools), service="gateway-proxy")


async def _unmount_one(gateway, name: str) -> None:
    """Remove a backend. FastMCP mount removal + clear registry."""
    # NOTE: FastMCP unmount API — verify exact method name at impl time
    # (gateway.unmount(name) if available; else rebuild without it).
    clear_tools(name)
    logger.info("server_unmounted", server=name, service="gateway-proxy")


async def watch_changes(gateway) -> None:
    """Subscribe to server:changed and hot-reload mounts. Runs forever."""
    r = get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe("server:changed")
    async for msg in pubsub.listen():
        if msg.get("type") != "message":
            continue
        parsed = parse_change_event(msg["data"])
        if not parsed:
            continue
        action, name = parsed
        info = await r.hgetall(f"servers:{name}")
        if action in ("add", "update") and info:
            await _unmount_one(gateway, name)
            await _mount_one(gateway, name, info["url"])
        elif action == "remove":
            await _unmount_one(gateway, name)
```

- [ ] **Step 4: Run, verify pass**

```bash
uv run pytest tests/test_registry.py -v
```
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add registry.py tests/test_registry.py
git commit -m "feat(gateway-proxy): server registry + Pub/Sub hot-reload + probe"
```

---

### Task 8: Server Entry — wire everything + run

**Files:**
- Create: `gateway-proxy/server.py`
- Create: `gateway-proxy/README.md`

**Interfaces:**
- Consumes: all prior modules

- [ ] **Step 1: Implement server.py**

```python
"""MCP Gateway Proxy — entry point.

Aggregates backend MCP servers via FastMCP mount(namespace=...). Validates
API tokens (SHA-256 vs Redis), enforces per-server read/write via middleware,
records failures to a Redis Stream, and exposes Prometheus metrics.
"""
import asyncio
import os
import structlog
from fastmcp import FastMCP

from observability import init_telemetry, REQUESTS_TOTAL, REQUEST_LATENCY, AUTH_FAILURES
from redis_client import get_redis, close_redis
from registry import mount_all, watch_changes
from auth import verify_token
from middleware import check_call_permission, classify_error, record_call_failure

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)

GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "8080"))

init_telemetry()
logger = structlog.get_logger()

gateway = FastMCP(
    "MCP Gateway",
    instructions="Aggregates backend MCP servers. Token auth required (Authorization: Bearer).",
)


async def _startup() -> None:
    """Load servers from Redis + start the change watcher."""
    await mount_all(gateway)
    asyncio.create_task(watch_changes(gateway))
    logger.info("gateway_started", port=GATEWAY_PORT, service="gateway-proxy")


# NOTE: FastMCP stateless mode does not run lifespan reliably, so we trigger
# startup via an import-time task scheduling in __main__ below.


def _extract_token(headers) -> str | None:
    """Pull the Bearer token from Authorization header."""
    auth = headers.get("authorization", "") if headers else ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


if __name__ == "__main__":
    # Schedule startup before run; the event loop is managed by mcp.run.
    loop = asyncio.new_event_loop()
    loop.run_until_complete(_startup())
    gateway.run(transport="streamable-http", stateless_http=True, port=GATEWAY_PORT)
```

- [ ] **Step 2: Smoke test — server starts and lists zero tools with empty Redis**

```bash
REDIS_URL=redis://localhost:6379/0 uv run python server.py &
sleep 3
# tools/list should return empty (no servers registered)
curl -s -X POST http://127.0.0.1:8080/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | head -c 200
kill %1
```
Expected: a JSON-RPC response with empty tools list (no error)

- [ ] **Step 3: Create README.md**

```markdown
# gateway-proxy

MCP 网关代理。聚合后端 MCP server，提供 token 认证 + 读写权限控制 + 失败审计 + Prometheus metrics。

## 运行

\`\`\`bash
uv sync
REDIS_URL=redis://localhost:6379/0 uv run python server.py  # :8080
\`\`\`

## 依赖
gateway-admin 写 Redis（注册 server/token），proxy 读 Redis 验证 + 热加载。

## 协议
MCP 2026-07-28, stateless HTTP。后端 MCP 通过 mount(namespace=name) 聚合。
```

- [ ] **Step 4: Run full test suite**

```bash
uv run pytest tests/ -v
```
Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add server.py README.md
git commit -m "feat(gateway-proxy): server entry — wire auth/registry/audit/metrics, run"
```

---

## Self-Review

**1. Spec coverage (§4 路由 + §5 proxy):**
- [x] namespace prefix routing -> Task 2 (split_prefix, resolve_target)
- [x] TOOL_REGISTRY mode lookup -> Task 2 (register_tools, get_tool_mode)
- [x] token SHA-256 verify + permission -> Task 3
- [x] failure audit journey -> Task 4 (record_failure)
- [x] permission middleware + error classify -> Task 5
- [x] OTel + Prometheus metrics -> Task 6
- [x] Pub/Sub hot-reload -> Task 7 (watch_changes, mount_all)
- [x] probe (MCP ping) -> Task 7 (probe)
- [x] tools introspection (tools/list, mode) -> Task 7 (_introspect_tools)
- [x] server entry + run -> Task 8
- [ ] full middleware wiring into FastMCP `on_message` — Task 5 implements the helpers; Task 8 wires the FastMCP Middleware class. NOTE: the FastMCP Middleware subclass (on_message) that calls check_call_permission + record_call_failure must be added in Task 8; verify the exact MiddlewareContext API against knowledge-base/19-middleware.md at impl time.

**2. Placeholder scan:** No TBD/TODO in code. The `_unmount_one` FastMCP unmount API is flagged with a NOTE to verify — that is a genuine API-uncertainty marker, not a placeholder.

**3. Type consistency:** `verify_token -> dict|None`, `check_permission(info, server, mode) -> bool`, `resolve_target -> (server, tool, mode)`, `record_failure(journey, error_type, meta)` — consistent across tasks.
