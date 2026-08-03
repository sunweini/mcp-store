# Multi-Search MCP 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 mcpstore 仓库新增三个独立搜索 MCP（tavily-mcp / brave-mcp / serpapi-mcp），每源支持多 API key 池（Redis 驱动 + 轮换 + 欠费剔除 + 低配额告警/兜底），gateway-admin 新增 API Keys 管理模块（CRUD + 探活 + 用量看板），zabbix-mcp 端口迁移 8000→9053。

**Architecture:** 三源独立 FastMCP server（方案 B，无聚合层），各自从 Redis `search:keys:<provider>` 加载 key 池，Pub/Sub 热更新；gateway-admin 写 key 后 PUBLISH 通知。前台展示 key 状态/配额/用量。全部注册进 gateway，MCP 容器内端口统一 9050-9500。

**Tech Stack:** FastMCP 4.0.0b1 + Python >=3.12 + uv（prerelease=allow）+ httpx + redis.asyncio + structlog + OpenTelemetry + FastAPI（admin）+ Vue 3（admin-ui）。

## Global Constraints

- MCP 容器内端口规范 9050-9500（见根 CLAUDE.md 端口表）：**tavily-mcp=9050、brave-mcp=9051、serpapi-mcp=9052、zabbix-mcp=9053**；不映射宿主端口
- FastMCP v4（`fastmcp==4.0.0b1`）+ MCP Protocol `2026-07-28`，streamable-http + `stateless_http=True`
- 依赖固定：`fastmcp==4.0.0b1`、`httpx>=0.27,<1.0`、`redis>=5.0`、`structlog>=24.0`、`opentelemetry-*`、`prometheus-client`（镜像 zabbix-mcp/pyproject.toml 清单）
- server name 小写字母/数字/连字符，**禁止下划线**：`tavily-mcp` / `brave-mcp` / `serpapi-mcp`
- 只读工具标 `ToolAnnotations(readOnlyHint=True)`；本 MCP 全部为读操作，无 destructiveHint
- 结构化日志（structlog key=value），每条带 service/trace_id/request_id/route；错误必带 `error` key
- key_id / api key 本身**禁止**入 metric label 与日志（高基数 + 敏感，OBS-CORE-003）
- Redis schema 前缀 `search:`（与 gateway 既有 `servers:`/`tokens:` 并列，不冲突）
- 每源工具名带源前缀：`tavily_search` / `brave_web_search` / `serpapi_google` 等（spec 已确认）
- KeyPool 实现复制三份（每源一个，遵守"每 MCP 独立目录独立发布"），不共享包
- 代码注释写"为什么"不写"做了什么"（OBS-CORE-005）
- 可观测性遵循 `~/.claude/docs/observability-coding-standards.md`

---

## 文件结构

### 每个新 MCP（tavily-mcp / brave-mcp / serpapi-mcp）目录

```
<mcp>/ 
├── CLAUDE.md           # 新建 — MCP 级开发说明
├── README.md           # 新建 — 功能说明（给用户看）
├── RELEASE.md          # 新建 — 发布指南（模板占位，部署任务再完善）
├── server.py           # 新建 — FastMCP 入口（镜像 zabbix-mcp/server.py）
├── logging_config.py   # 复制 zabbix-mcp/logging_config.py
├── telemetry.py        # 新建 — 镜像 zabbix-mcp/telemetry.py，指标换 search_* 
├── key_pool.py         # 新建 — KeyPool（三份同逻辑，错误映射不同）
├── <provider>_client.py # 新建 — API client（tavily_client.py / brave_client.py / serpapi_client.py）
├── tools/__init__.py   # 新建 — register_tools + _metrics_wrapper（镜像 zabbix-mcp）
├── tools/<tools>.py    # 新建 — 工具实现（模块级函数，tests 可直接 import）
├── pyproject.toml      # 新建 — 镜像 zabbix-mcp
└── tests/              # 新建 — conftest + test_key_pool + test_<provider>_client + test_tools
```

### 修改现有文件

| 文件 | 改动 |
|---|---|
| `gateway-admin/api/keys.py` | 新建 — search-keys API 路由 |
| `gateway-admin/app.py` | 注册 keys router |
| `gateway-admin/admin-ui/src/api/index.js` | 加 search-keys API 函数 |
| `gateway-admin/admin-ui/src/views/APIKeys.vue` | 新建 — key 管理页 |
| `gateway-admin/admin-ui/src/router/index.js` | 加 /api-keys 路由 |
| `gateway-admin/admin-ui/src/components/Sidebar.vue` | 加 API Keys 菜单 |
| `deploy/docker-compose.yml` | 加 3 个新服务 + zabbix-mcp MCP_PORT→9053 |
| `deploy/init.sh` | 注册 3 个新 server |
| `zabbix-mcp/README.md` | 端口 8000→9053（已改，本次验证） |

---

## Task 1: tavily-mcp KeyPool + TavilyClient + 单元测试

**Files:**
- Create: `tavily-mcp/key_pool.py`, `tavily-mcp/tavily_client.py`, `tavily-mcp/tests/conftest.py`, `tavily-mcp/tests/test_key_pool.py`, `tavily-mcp/tests/test_tavily_client.py`
- Create: `tavily-mcp/pyproject.toml`（镜像 zabbix-mcp，dependencies 加 `redis>=5.0`，name 改 `tavily-mcp`）

**Interfaces:**
- Consumes: 无（首个 MCP，先建目录与依赖）
- Produces:
  - `KeyPool(provider: str, redis, pubsub, quota_default: int)` — 见下方方法签名
  - `TavilyClient(key: str, timeout: float = 5.0)` — `async search(params) / extract / crawl / map / research` 返回 dict，`async usage() -> dict`，`async close()`

- [ ] **Step 1: 搭建目录与 pyproject**

```bash
mkdir -p tavily-mcp/tools tavily-mcp/tests
cp zabbix-mcp/logging_config.py tavily-mcp/
```

`tavily-mcp/pyproject.toml`（镜像 zabbix-mcp，改动处标注）：

```toml
[tool.uv]
prerelease = "allow"

[project]
name = "tavily-mcp"
version = "0.1.0"
description = "Tavily search MCP — multi-key pool, search/extract/crawl/map/research"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "fastmcp==4.0.0b1",
    "httpx>=0.27,<1.0",
    "redis>=5.0",
    "structlog>=24.0",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-exporter-prometheus",
    "prometheus-client",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.24", "aresponses>=0.4.0"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

```bash
cd tavily-mcp && uv sync --prerelease=allow
```

- [ ] **Step 2: 写 KeyPool 失败测试**（`tests/test_key_pool.py`）

```python
"""KeyPool unit tests — rotation, failover, cooldown, hot reload."""
import json
import time
from unittest.mock import AsyncMock

import pytest
from key_pool import ErrorKind, KeyPool


def _rec(key_id, key, **over):
    base = {
        "key": key, "provider": "tavily", "enabled": True,
        "monthly_quota": 1000, "status": "active",
        "cooldown_until": None, "remaining": None, "last_error": None,
    }
    base.update(over)
    return json.dumps(base)


class FakeRedis:
    """Minimal async Redis fake with the methods KeyPool uses."""

    def __init__(self, records: dict[str, str]):
        self._records = dict(records)
        self.hset_calls = []
        self.zadd_calls = []
        self.expire_calls = []

    async def hgetall(self, name):
        return dict(self._records)

    async def hset(self, name, mapping):
        self._records[name] = mapping
        self.hset_calls.append((name, mapping))
        return 1

    async def zadd(self, name, mapping):
        self.zadd_calls.append((name, mapping))
        return 1

    async def expire(self, name, seconds):
        self.expire_calls.append((name, seconds))
        return True


@pytest.fixture
def pool():
    records = {
        "k1": _rec("k1", "tvly-a", status="active", remaining=900),
        "k2": _rec("k2", "tvly-b", status="active", remaining=800),
    }
    fake_redis = FakeRedis(records)
    pubsub = AsyncMock()
    pool = KeyPool("tavily", fake_redis, pubsub, quota_default=1000)
    return pool, fake_redis


async def test_next_key_prefers_higher_remaining(pool):
    pool_, _ = pool
    key = await pool_.next_key()
    assert key is not None
    assert key["key"] == "tvly-a"


async def test_next_key_skips_cooldown(pool):
    pool_, _ = pool
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 300))
    pool_._records["k1"]["status"] = "cooldown"
    pool_._records["k1"]["cooldown_until"] = future
    key = await pool_.next_key()
    assert key["key"] == "tvly-b"


async def test_next_key_returns_none_when_all_unavailable(pool):
    pool_, _ = pool
    for r in pool_._records.values():
        r["status"] = "invalid"
    assert await pool_.next_key() is None


async def test_on_error_invalid_marks_invalid(pool):
    pool_, _ = pool
    await pool_.on_error("k1", ErrorKind.INVALID)
    assert pool_._records["k1"]["status"] == "invalid"


async def test_on_error_rate_limit_sets_cooldown(pool):
    pool_, _ = pool
    await pool_.on_error("k1", ErrorKind.RATE_LIMIT)
    assert pool_._records["k1"]["status"] == "cooldown"
    assert pool_._records["k1"]["cooldown_until"] is not None


async def test_on_error_exhausted_sets_zero_remaining(pool):
    pool_, _ = pool
    await pool_.on_error("k1", ErrorKind.EXHAUSTED)
    assert pool_._records["k1"]["status"] == "exhausted"
    assert pool_._records["k1"]["remaining"] == 0


async def test_low_quota_skipped_but_fallback_used(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "low_quota"
    pool_._records["k1"]["remaining"] = 40
    pool_._records["k2"]["status"] = "invalid"
    key = await pool_.next_key()
    assert key["key"] == "tvly-a"  # fallback: only low_quota left


async def test_low_quota_skipped_when_healthy_others_exist(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "low_quota"
    pool_._records["k1"]["remaining"] = 40
    key = await pool_.next_key()
    assert key["key"] == "tvly-b"


async def test_low_quota_warning_participates(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "low_quota_warning"
    pool_._records["k1"]["remaining"] = 80
    key = await pool_.next_key()
    assert key is not None


async def test_unknown_quota_does_not_trigger_low(pool):
    pool_, _ = pool
    pool_._records["k1"]["status"] = "active"
    pool_._records["k1"]["remaining"] = None
    pool_._records["k2"]["status"] = "invalid"
    key = await pool_.next_key()
    assert key["key"] == "tvly-a"  # unknown → treated normal


async def test_on_success_records_usage_and_resets(pool):
    pool_, _ = pool
    await pool_.on_success("k1", remaining=890)
    assert pool_._records["k1"]["status"] == "active"
    assert pool_._records["k1"]["cooldown_until"] is None
    assert pool_._records["k1"]["remaining"] == 890
    assert pool_._records["k1"]["last_used_at"] is not None


async def test_reload_refreshes_records(pool):
    pool_, fake_redis = pool
    fake_redis._records["k3"] = _rec("k3", "tvly-c")
    await pool_.reload()
    assert "k3" in pool_._records
```

- [ ] **Step 3: 运行确认失败**

Run: `cd tavily-mcp && uv run pytest tests/test_key_pool.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'key_pool'`

- [ ] **Step 4: 实现 KeyPool**（`key_pool.py`）

```python
"""Single-provider API key pool — Redis-driven rotation + failover.

One copy per MCP (tavily/brave/serpapi); error-kind mapping differs per
provider, rotation logic identical. Design: keys live in Redis
(search:keys:<provider>) so gateway-admin can manage them at runtime and
all MCP instances share the same pool. Pub/Sub channel search:keys:channel
triggers hot reload so admin edits take effect without restart.

OBS: key_id 与 key 明文均不得写入日志/metrics（高基数+敏感）。
"""
import json
import time
import uuid
from enum import Enum

import structlog

logger = structlog.get_logger()

LOW_QUOTA_RATIO = 0.05      # remaining/quota < 5% → skip, fallback only
WARN_QUOTA_RATIO = 0.10     # remaining/quota < 10% → warning, still used
DEFAULT_COOLDOWN_SECONDS = 30
RETRY_AFTER_LIMIT = 600     # cap Retry-After to avoid absurd cooldowns


class ErrorKind(str, Enum):
    INVALID = "invalid"          # 401/403 — key 失效，永久剔除
    EXHAUSTED = "exhausted"      # 配额耗尽/欠费 — 永久剔除
    RATE_LIMIT = "rate_limit"    # 429 — 冷却后恢复


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _key_id(provider: str) -> str:
    """URL-safe opaque id, not derived from the key itself."""
    return f"{provider}_{uuid.uuid4().hex[:12]}"


class KeyPool:
    """Redis-backed key pool for one provider.

    redis: async redis client (decode_responses=True).
    pubsub: async PubSub object on channel search:keys:channel.
    quota_default: monthly quota used when a key lacks monthly_quota.
    """

    def __init__(self, provider: str, redis, pubsub, quota_default: int):
        self.provider = provider
        self._redis = redis
        self._pubsub = pubsub
        self._quota_default = quota_default
        self._records: dict[str, dict] = {}
        self._key_hash: dict[str, str] = {}  # key → key_id (decorrelation)
        self._pool_key = f"search:keys:{provider}"

    async def start(self) -> None:
        await self.reload()
        # Subscribe in a background task; messages trigger reload().
        import asyncio
        asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        while True:
            try:
                msg = await self._pubsub.get_message(ignore_subscribe=True, timeout=30)
                if msg and msg.get("type") == "message":
                    await self.reload()
            except Exception:
                # Redis 短暂故障不致命 — 保留现有池，等待下次通知
                await asyncio.sleep(5)

    async def reload(self) -> None:
        raw = await self._redis.hgetall(self._pool_key)
        records: dict[str, dict] = {}
        key_hash: dict[str, str] = {}
        for key_id, payload in raw.items():
            try:
                rec = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning("key_pool_skip_bad_record",
                               provider=self.provider, error="bad_json")
                continue
            rec["key_id"] = key_id
            records[key_id] = rec
            key_hash[rec["key"]] = key_id
        self._records = records
        self._key_hash = key_hash
        logger.info("key_pool_reloaded",
                    provider=self.provider, key_count=len(records))

    async def next_key(self) -> dict | None:
        """Pick the best key. Priority:
        1. enabled, status != invalid/exhausted, cooldown expired
        2. skip low_quota unless no healthy key remains (fallback)
        3. highest remaining (quota-aware), tie by insertion order
        Returns the full record dict, or None if pool empty.
        """
        now = time.time()
        healthy, low_quota, unavailable = [], [], []
        for rec in self._records.values():
            if not rec.get("enabled", True) or rec.get("status") in ("invalid", "exhausted"):
                unavailable.append(rec)
                continue
            if rec.get("status") == "cooldown":
                until = rec.get("cooldown_until")
                if until and _parse_iso(until) > now:
                    unavailable.append(rec)
                    continue
                rec["status"] = "active"
                rec["cooldown_until"] = None
            ratio = self._ratio(rec)
            if ratio is not None and ratio < LOW_QUOTA_RATIO:
                rec["status"] = "low_quota"
                low_quota.append(rec)
            else:
                if ratio is not None and ratio < WARN_QUOTA_RATIO:
                    rec["status"] = "low_quota_warning"
                healthy.append(rec)

        candidates = healthy if healthy else low_quota
        if not candidates:
            return None
        candidates.sort(key=lambda r: r.get("remaining") or 0, reverse=True)
        return candidates[0]

    async def on_success(self, key_id: str, remaining: int | None = None) -> None:
        rec = self._records.get(key_id)
        if rec is None:
            return
        rec["status"] = "active"
        rec["cooldown_until"] = None
        rec["last_used_at"] = _now_iso()
        rec["last_error"] = None
        if remaining is not None:
            rec["remaining"] = remaining
        await self._write(key_id, rec)
        # 本地用量计数：ZSet member=now, score=now（按月窗口统计）
        now = time.time()
        await self._redis.zadd(f"search:usage:{self.provider}:{key_id}", {str(now): now})
        await self._redis.expire(f"search:usage:{self.provider}:{key_id}", 60 * 24 * 32)

    async def on_error(self, key_id: str, kind: ErrorKind,
                       retry_after: int | None = None) -> None:
        rec = self._records.get(key_id)
        if rec is None:
            return
        if kind == ErrorKind.INVALID:
            rec["status"] = "invalid"
            rec["cooldown_until"] = None
        elif kind == ErrorKind.EXHAUSTED:
            rec["status"] = "exhausted"
            rec["cooldown_until"] = None
            rec["remaining"] = 0
        elif kind == ErrorKind.RATE_LIMIT:
            rec["status"] = "cooldown"
            seconds = min(retry_after or DEFAULT_COOLDOWN_SECONDS, RETRY_AFTER_LIMIT)
            rec["cooldown_until"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))
        rec["last_error"] = kind.value
        await self._write(key_id, rec)

    async def probe(self, key: str) -> dict:
        """Probe a key at add-time. Returns record dict with status
        active/invalid + remaining. Subclasses override to call provider API."""
        raise NotImplementedError

    def _ratio(self, rec: dict) -> float | None:
        quota = rec.get("monthly_quota") or self._quota_default
        remaining = rec.get("remaining")
        if remaining is None or quota <= 0:
            return None
        return remaining / quota

    async def _write(self, key_id: str, rec: dict) -> None:
        await self._redis.hset(self._pool_key, key_id, json.dumps(rec, ensure_ascii=False))


def _parse_iso(iso: str) -> float:
    """Parse our own %Y-%m-%dT%H:%M:%SZ format (no external dep)."""
    return time.mktime(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd tavily-mcp && uv run pytest tests/test_key_pool.py -v`
Expected: PASS（13 个测试）

- [ ] **Step 6: 写 TavilyClient 失败测试**（`tests/test_tavily_client.py`）

```python
"""TavilyClient tests — endpoints, auth, error mapping, usage."""
import httpx
import pytest

from tavily_client import TavilyClient, classify_error


async def test_search_success(mock_transport):
    client = TavilyClient("tvly-test", transport=mock_transport(
        {"results": [{"title": "t", "url": "https://x"}]}))
    result = await client.search({"query": "ping", "max_results": 1})
    assert result["results"][0]["title"] == "t"
    assert mock_transport.last_request.headers["Authorization"] == "Bearer tvly-test"
    await client.close()


async def test_search_401_classified_invalid(mock_transport):
    client = TavilyClient("tvly-test", transport=mock_transport(status_code=401))
    with pytest.raises(Exception) as ei:
        await client.search({"query": "q"})
    assert classify_error(ei.value) == "invalid"
    await client.close()


async def test_search_429_classified_rate_limit(mock_transport):
    client = TavilyClient("tvly-test", transport=mock_transport(
        status_code=429, headers={"Retry-After": "45"}))
    with pytest.raises(Exception):
        await client.search({"query": "q"})
    await client.close()


async def test_usage_returns_remaining(mock_transport):
    client = TavilyClient("tvly-test", transport=mock_transport(
        {"plan_usage": {"search": {"remaining": 987}}}))
    usage = await client.usage()
    assert usage["plan_usage"]["search"]["remaining"] == 987
    await client.close()
```

- [ ] **Step 7: conftest mock transport**（`tests/conftest.py`）

```python
"""Shared httpx mock transport + pool fixture for tavily tests."""
import json
import pytest
import httpx

from key_pool import KeyPool
from tavily_client import TavilyClient


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self._status_code = status_code
        self._headers = headers or {}
        self.last_request = None

    async def handle_async_request(self, request):
        self.last_request = request
        return httpx.Response(
            self._status_code, json=self._payload,
            headers=self._headers, request=request)


@pytest.fixture
def mock_transport():
    def factory(payload=None, status_code=200, headers=None):
        return MockTransport(payload or {}, status_code, headers)
    return factory
```

- [ ] **Step 8: 实现 TavilyClient**（`tavily_client.py`）

```python
"""Tavily REST API client.

Endpoints: POST https://api.tavily.com/{search|extract|crawl|map|research}
Auth: Bearer token. Usage: GET /usage (官方剩余配额).

Error classification (per spec 错误语义映射):
- 401/403 → INVALID（key 失效，永久剔除）
- 429 → RATE_LIMIT（Retry-After 头 → cooldown）
- 其余 4xx/5xx/网络错误 → raise（工具层不重试）
"""
import time

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from key_pool import ErrorKind

logger = structlog.get_logger()
tracer = trace.get_tracer("tavily_mcp.tavily_client")

API_BASE = "https://api.tavily.com"
RETRYABLE_IF_IDEMPOTENT = {"search", "extract", "map"}


def classify_error(exc: Exception, status_code: int | None = None) -> ErrorKind | None:
    """Map HTTP error to pool ErrorKind, or None if not pool-relevant."""
    if status_code in (401, 403):
        return ErrorKind.INVALID
    if status_code == 429:
        return ErrorKind.RATE_LIMIT
    return None


class TavilyError(Exception):
    """Tavily API business error (non-2xx with body)."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"tavily api error {status_code}: {detail}")


class TavilyClient:
    """Thin async client. transport injectable for tests (httpx ASGI/mock)."""

    def __init__(self, key: str, timeout: float = 5.0, transport=None):
        self._key = key
        self._timeout = timeout
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {key}"},
            transport=transport,
        )

    async def search(self, params: dict) -> dict:
        return await self._post("search", params)

    async def extract(self, params: dict) -> dict:
        return await self._post("extract", params)

    async def crawl(self, params: dict) -> dict:
        return await self._post("crawl", params)

    async def map(self, params: dict) -> dict:
        return await self._post("map", params)

    async def research(self, params: dict) -> dict:
        return await self._post("research", params)

    async def usage(self) -> dict:
        resp = await self._http.get(f"{API_BASE}/usage")
        resp.raise_for_status()
        return resp.json()

    async def _post(self, endpoint: str, params: dict) -> dict:
        with tracer.start_as_current_span(f"tavily_client.{endpoint}") as span:
            span.set_attributes({"http.method": "POST", "http.url": f"{API_BASE}/{endpoint}"})
            start = time.monotonic()
            resp = await self._http.post(f"{API_BASE}/{endpoint}", json=params)
            duration = time.monotonic() - start
            span.set_attribute("http.status_code", resp.status_code)
            if resp.status_code >= 400:
                span.set_status(Status(StatusCode.ERROR, f"tavily {resp.status_code}"))
                logger.error("tavily_api_error",
                             service="tavily-mcp",
                             endpoint=endpoint,
                             http_status=resp.status_code,
                             error=resp.text[:200],
                             duration_ms=round(duration * 1000))
                raise TavilyError(resp.status_code, resp.text[:200])
            return resp.json()

    async def close(self) -> None:
        await self._http.aclose()
```

- [ ] **Step 9: 跑测试确认通过**

Run: `cd tavily-mcp && uv run pytest tests/ -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add tavily-mcp/
git commit -m "feat(tavily-mcp): KeyPool + TavilyClient with tests"
```

---

## Task 2: tavily-mcp 工具层 + server.py + telemetry

**Files:**
- Create: `tavily-mcp/tools/__init__.py`, `tavily-mcp/tools/search.py`, `tavily-mcp/server.py`, `tavily-mcp/telemetry.py`, `tavily-mcp/tests/test_tools.py`, `tavily-mcp/CLAUDE.md`, `tavily-mcp/README.md`
- Modify: `tavily-mcp/tests/conftest.py`（加 app 级别 pool fixture）

**Interfaces:**
- Consumes: `KeyPool.next_key()/on_success()/on_error()`、`TavilyClient`（Task 1）、`classify_error()`、`ErrorKind`
- Produces:
  - `tools/search.py`: `register(mcp, get_pool)` — 注册 5 个工具
  - 工具函数（模块级，可测试）：`tavily_search(query, search_depth, topic, days, max_results, include_answer, include_raw_content, include_images, *, pool)` 等
  - `server.py` 暴露 `mcp` 实例 + `main`（streamable-http, port 9050）
  - `telemetry.py`: `init_telemetry(service_name="tavily-mcp")` + 指标 `SEARCH_REQUESTS_TOTAL`（provider/engine/status 标签）、`SEARCH_QUOTA_REMAINING`、`SEARCH_QUOTA_RATIO`、`SEARCH_KEY_POOL_SIZE`、`SEARCH_KEY_INVALID_TOTAL`、`SEARCH_REQUEST_DURATION`

- [ ] **Step 1: 写工具失败测试**（`tests/test_tools.py`）

```python
"""Tool layer tests — parameter passthrough, pool integration, errors."""
from unittest.mock import AsyncMock

import pytest
from key_pool import ErrorKind
from tools.search import tavily_search, tavily_research


class FakeClient:
    def __init__(self, result=None):
        self.result = result or {"results": [{"title": "t", "url": "u"}]}
        self.called_with = None

    async def search(self, params):
        self.called_with = params
        return self.result

    async def research(self, params):
        self.called_with = params
        return {"response": "answer", "sources": []}


class FakePool:
    def __init__(self, records=None):
        self.records = records or [{
            "key_id": "k1", "key": "tvly-test", "provider": "tavily",
            "monthly_quota": 1000, "status": "active", "remaining": 900,
        }]
        self.errors = []
        self.successes = []

    async def next_key(self):
        return self.records[0] if self.records else None

    async def on_success(self, key_id, remaining=None):
        self.successes.append(key_id)

    async def on_error(self, key_id, kind, retry_after=None):
        self.errors.append((key_id, kind))


def _pool():
    return FakePool()


async def test_tavily_search_passes_params():
    pool = _pool()
    result = await tavily_search("hello world", max_results=3, pool=pool)
    assert result["status"] == "ok"


async def test_tavily_search_invalid_params_returns_error():
    pool = _pool()
    result = await tavily_search("", pool=pool)  # empty query
    assert result["status"] == "error"


async def test_tavily_search_no_keys_returns_error():
    pool = FakePool(records=[])
    result = await tavily_search("q", pool=pool)
    assert result["status"] == "error"
    assert "不可用" in result["message"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd tavily-mcp && uv run pytest tests/test_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: tools.search`

- [ ] **Step 3: 实现 tools/search.py**

```python
"""Tavily search tools.

Design note: functions at module level (not closures) so tests import
and call them directly with a FakePool. register() creates thin MCP
wrappers injecting the real pool.
"""
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
import structlog

from key_pool import ErrorKind
from tavily_client import TavilyClient, TavilyError, classify_error

logger = structlog.get_logger()

# 幂等轻查询自动重试；crawl/research 长任务不重试（spec 错误处理节）
RETRYABLE = {"tavily_search", "tavily_extract", "tavily_map"}
NO_RETRY = {"tavily_crawl", "tavily_research"}


async def _call_with_pool(pool, tool_name: str, endpoint: str, params: dict) -> dict:
    """Pick key → call API → report result to pool. One retry on failover.

    Returns the tool response dict (status ok/error).
    """
    key_rec = await pool.next_key()
    if key_rec is None:
        return {"status": "error",
                "message": "tavily 该源所有 API key 不可用，请在前台检查 key 池状态"}
    client = TavilyClient(key_rec["key"])
    try:
        resp = await client._post(endpoint, params)
        await pool.on_success(key_rec["key_id"])
        return {"status": "ok", "data": resp}
    except Exception as exc:
        kind = classify_error(exc, getattr(exc, "status_code", None))
        await pool.on_error(key_rec["key_id"], kind or ErrorKind.EXHAUSTED)
        if tool_name in RETRYABLE and pool._records:
            # 换下一 key 重试一次（幂等操作才允许）
            key_rec2 = await pool.next_key()
            if key_rec2 and key_rec2["key_id"] != key_rec["key_id"]:
                client2 = TavilyClient(key_rec2["key"])
                try:
                    resp = await client2._post(endpoint, params)
                    await pool.on_success(key_rec2["key_id"])
                    return {"status": "ok", "data": resp}
                except Exception as exc2:
                    kind2 = classify_error(exc2, getattr(exc2, "status_code", None))
                    await pool.on_error(key_rec2["key_id"], kind2 or ErrorKind.EXHAUSTED)
        return {"status": "error", "message": str(exc)}


async def tavily_search(
    query: str,
    search_depth: str = "basic",
    topic: str = "general",
    days: int | None = None,
    max_results: int = 5,
    include_answer: bool = False,
    include_raw_content: bool = False,
    include_images: bool = False,
    *,
    pool,
) -> dict:
    """Web search via Tavily. Returns organic results with title/url/content.

    query: 搜索词。search_depth: basic/advanced。topic: general/news/finance。
    max_results: 1-20。include_answer: 附带 AI 摘要答案。
    """
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    params = {
        "query": query,
        "search_depth": search_depth,
        "topic": topic,
        "max_results": min(max_results, 20),
        "include_answer": include_answer,
        "include_raw_content": include_raw_content,
        "include_images": include_images,
    }
    if days is not None:
        params["days"] = days
    return await _call_with_pool(pool, "tavily_search", "search", params)


async def tavily_extract(urls: list[str], extract_depth: str = "basic", *, pool) -> dict:
    """Extract clean text content from URLs. urls: 1-10 个 URL 列表。"""
    if not urls:
        return {"status": "error", "message": "urls 不能为空"}
    params = {"urls": urls[:10], "extract_depth": extract_depth}
    return await _call_with_pool(pool, "tavily_extract", "extract", params)


async def tavily_crawl(urls: list[str], max_depth: int = 3, max_pages: int = 20,
                       max_cost: float = 10.0, *, pool) -> dict:
    """Crawl websites, return structured data. 长任务 — 不自动重试。"""
    if not urls:
        return {"status": "error", "message": "urls 不能为空"}
    params = {"urls": urls[:5], "max_depth": max_depth,
              "max_pages": max_pages, "max_cost": max_cost}
    return await _call_with_pool(pool, "tavily_crawl", "crawl", params)


async def tavily_map(query: str, search_depth: str = "basic", max_results: int = 100,
                     *, pool) -> dict:
    """Map search — return URLs across many topics for a query."""
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    params = {"query": query, "search_depth": search_depth, "max_results": min(max_results, 100)}
    return await _call_with_pool(pool, "tavily_map", "map", params)


async def tavily_research(query: str, max_depth: int = 3, max_learnings: int = 5,
                          max_sources: int = 5, max_browser_pages: int = 20,
                          *, pool) -> dict:
    """Deep research — gather info from multiple sources, return answer. 长任务不重试。"""
    if not query.strip():
        return {"status": "error", "message": "query 不能为空"}
    params = {"query": query, "max_depth": max_depth, "max_learnings": max_learnings,
              "max_sources": max_sources, "max_browser_pages": max_browser_pages}
    return await _call_with_pool(pool, "tavily_research", "research", params)


def register(mcp: FastMCP, get_pool, metrics=None) -> None:
    """Register all tavily tools. get_pool: callable returning the KeyPool."""
    _wrap = metrics or (lambda name: lambda f: f)

    def _make(name, func):
        async def _mcp(*args, **kwargs):
            return await func(*args, **kwargs, pool=get_pool())
        _mcp.__doc__ = func.__doc__
        mcp.tool(
            name=name,
            description=func.__doc__,
            annotations=ToolAnnotations(readOnlyHint=True),
        )(_wrap(name)(_mcp))

    _make("tavily_search", tavily_search)
    _make("tavily_extract", tavily_extract)
    _make("tavily_crawl", tavily_crawl)
    _make("tavily_map", tavily_map)
    _make("tavily_research", tavily_research)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd tavily-mcp && uv run pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: 写 telemetry.py**

镜像 `zabbix-mcp/telemetry.py`（`init_telemetry(service_name)` + OTLP/Prometheus 设置），指标改为 spec 可观测性节：

```python
"""OpenTelemetry TracerProvider + search-metrics setup.

镜像 zabbix-mcp/telemetry.py；指标按 spec 可观测性节：
- SEARCH_REQUESTS_TOTAL{provider, engine, status} — status 低基数: success/rate_limit/invalid/exhausted/timeout
- SEARCH_QUOTA_REMAINING{provider} — 池内最低剩余
- SEARCH_QUOTA_RATIO{provider, level} — warning<10%/critical<5%/exhausted=0（按 provider 聚合，无 key label）
- SEARCH_KEY_POOL_SIZE{provider}, SEARCH_KEY_INVALID_TOTAL{provider}
- SEARCH_REQUEST_DURATION histogram — bucket 对齐 SLO: 0.1/0.5/1/3/5
"""
import os

import structlog
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = structlog.get_logger()

PROMETHEUS_PORT = int(os.environ.get("PROMETHEUS_PORT", "9464"))

# Module-level instruments — guard with `if metric:` (may be None before init)
SEARCH_REQUESTS_TOTAL = None
SEARCH_QUOTA_REMAINING = None
SEARCH_QUOTA_RATIO = None
SEARCH_KEY_POOL_SIZE = None
SEARCH_KEY_INVALID_TOTAL = None
SEARCH_REQUEST_DURATION = None


def init_telemetry(service_name: str) -> None:
    """Initialize OTel + Prometheus metrics (same pattern as zabbix-mcp)."""
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})
    # Traces: OTLP exporter if configured, else ConsoleSpanExporter
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        span_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
    else:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        span_exporter = ConsoleSpanExporter()
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)

    # Metrics: Prometheus reader
    try:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        reader = PrometheusMetricReader()
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(meter_provider)
        meter = metrics.get_meter(service_name)
        global SEARCH_REQUESTS_TOTAL, SEARCH_QUOTA_REMAINING, SEARCH_QUOTA_RATIO
        global SEARCH_KEY_POOL_SIZE, SEARCH_KEY_INVALID_TOTAL, SEARCH_REQUEST_DURATION
        SEARCH_REQUESTS_TOTAL = meter.create_counter(
            "search_requests_total", unit="1", description="Search requests by provider/engine/status")
        SEARCH_QUOTA_REMAINING = meter.create_up_down_counter(
            "search_quota_remaining", unit="1", description="Lowest remaining quota in pool (provider)")
        SEARCH_QUOTA_RATIO = meter.create_up_down_counter(
            "search_quota_ratio", unit="1", description="Quota ratio bucket by provider (warning/critical/exhausted)")
        SEARCH_KEY_POOL_SIZE = meter.create_up_down_counter(
            "search_key_pool_size", unit="1", description="Active keys in pool (provider)")
        SEARCH_KEY_INVALID_TOTAL = meter.create_counter(
            "search_key_invalid_total", unit="1", description="Keys marked invalid (provider)")
        SEARCH_REQUEST_DURATION = meter.create_histogram(
            "search_request_duration_seconds", unit="s",
            description="Search request latency",
            explicit_bucket_boundaries=[0.1, 0.5, 1.0, 3.0, 5.0])
    except Exception as e:
        logger.warning("telemetry_metrics_disabled", service=service_name, error=str(e))
```

- [ ] **Step 6: 写 server.py**

镜像 `zabbix-mcp/server.py`，改：

```python
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
```

- [ ] **Step 7: 写 tools/__init__.py**

镜像 `zabbix-mcp/tools/__init__.py`（`_metrics_wrapper` 装饰器 + `register_tools(mcp, get_pool)`），指标名换 search_*：

```python
"""Tool registration — mirrors zabbix-mcp pattern."""
import time
import functools

from tools import search

try:
    from telemetry import (SEARCH_REQUESTS_TOTAL, SEARCH_REQUEST_DURATION,
                           SEARCH_KEY_INVALID_TOTAL)
except ImportError:
    SEARCH_REQUESTS_TOTAL = SEARCH_REQUEST_DURATION = SEARCH_KEY_INVALID_TOTAL = None


def _metrics_wrapper(tool_name: str):
    """Record search_requests_total / duration / invalid counter."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                if SEARCH_REQUESTS_TOTAL:
                    status = "success" if result.get("status") == "ok" else "error"
                    SEARCH_REQUESTS_TOTAL.add(
                        1, attributes={"provider": "tavily", "engine": tool_name, "status": status})
                return result
            except Exception:
                if SEARCH_REQUESTS_TOTAL:
                    SEARCH_REQUESTS_TOTAL.add(
                        1, attributes={"provider": "tavily", "engine": tool_name, "status": "error"})
                raise
            finally:
                duration = time.monotonic() - start
                if SEARCH_REQUEST_DURATION:
                    SEARCH_REQUEST_DURATION.record(duration, attributes={"provider": "tavily", "engine": tool_name})
        return wrapper
    return decorator


def register_tools(mcp, get_pool) -> None:
    search.register(mcp, get_pool, metrics=_metrics_wrapper)
```

- [ ] **Step 8: 全量测试 + 启动冒烟**

```bash
cd tavily-mcp && uv run pytest tests/ -v
# 冒烟: 起 Redis + server，探活
redis-cli -h localhost ping >/dev/null 2>&1 || redis-server --daemonize yes
REDIS_URL=redis://localhost:6379/0 uv run python -c "
import asyncio, os
os.environ['REDIS_URL']='redis://localhost:6379/0'
from server import _init_pool, mcp
pool = _init_pool()
print('pool provider:', pool.provider, 'keys:', len(pool._records))
"
```

Expected: 全部 PASS；冒烟输出 `pool provider: tavily keys: 0`

- [ ] **Step 9: 写 CLAUDE.md + README.md**

`tavily-mcp/CLAUDE.md`（镜像 zabbix-mcp/CLAUDE.md 骨架）：架构、env 表（REDIS_URL 必填 / MCP_PORT 9050 / TAVILY_QUOTA_DEFAULT 1000 / PROMETHEUS_PORT 9464）、KeyPool 设计说明、错误映射表、端口登记 9050。

`tavily-mcp/README.md`：功能说明（5 tools 表）、连接配置、key 管理入口（指向 gateway-admin API Keys 页）。

- [ ] **Step 10: Commit**

```bash
git add tavily-mcp/
git commit -m "feat(tavily-mcp): tools, server, telemetry, docs"
```

---

## Task 3: brave-mcp（KeyPool 复制 + BraveClient + 2 tools + server）

**Files:**
- Create: `brave-mcp/` 全套（结构同 tavily-mcp：key_pool.py、brave_client.py、server.py、telemetry.py、tools/、tests/、pyproject.toml、CLAUDE.md、README.md）
- Create: `brave-mcp/tools/web.py`

**Interfaces:**
- Consumes: Task 1/2 的 KeyPool 设计（复制，逻辑相同）
- Produces:
  - `BraveClient(key, timeout=5.0)` — `async web_search(params) / local_search(params)`，`async close()`；`classify_error(exc, status_code)` → INVALID(401)/RATE_LIMIT(429)
  - `tools/web.py`: `register(mcp, get_pool)` + 模块级 `brave_web_search(query, count, offset, *, pool)` / `brave_local_search(query, count, *, pool)`
  - `server.py` 用 `KeyPool("brave", ...)` + MCP_PORT 9051 + QUOTA_DEFAULT 2000（`BRAVE_QUOTA_DEFAULT`）

- [ ] **Step 1: 复制 tavily-mcp 骨架**

```bash
cp -r tavily-mcp brave-mcp
rm -rf brave-mcp/tests brave-mcp/tools brave-mcp/tavily_client.py
```

- [ ] **Step 2: 写 BraveClient 失败测试 + conftest**（`tests/test_brave_client.py`）

```python
"""BraveClient tests — endpoints, X-Subscription-Token auth, error mapping."""
import httpx
import pytest

from brave_client import BraveClient, classify_error


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload, status_code=200, headers=None):
        self._payload, self._status_code = payload, status_code
        self._headers = headers or {}
        self.last_request = None

    async def handle_async_request(self, request):
        self.last_request = request
        return httpx.Response(self._status_code, json=self._payload,
                              headers=self._headers, request=request)


async def test_web_search_success_and_auth_header():
    transport = MockTransport({"web": {"results": [{"title": "t", "url": "https://x"}]}})
    client = BraveClient("BSA-test", transport=transport)
    result = await client.web_search({"q": "hello", "count": 5})
    assert result["web"]["results"][0]["title"] == "t"
    assert transport.last_request.headers["X-Subscription-Token"] == "BSA-test"
    assert transport.last_request.url.path == "/res/v1/web/search"
    await client.close()


async def test_web_search_401_classified_invalid():
    client = BraveClient("BSA-test", transport=MockTransport({}, status_code=401))
    with pytest.raises(Exception):
        await client.web_search({"q": "q"})
    assert classify_error(Exception(), 401) == "invalid"
    await client.close()


async def test_web_search_429_classified_rate_limit():
    client = BraveClient("BSA-test", transport=MockTransport(
        {}, status_code=429, headers={"Retry-After": "30"}))
    with pytest.raises(Exception):
        await client.web_search({"q": "q"})
    assert classify_error(Exception(), 429) == "rate_limit"
    await client.close()


async def test_local_search_endpoint():
    transport = MockTransport({"local": {"results": [{"title": "t"}]}})
    client = BraveClient("BSA-test", transport=transport)
    result = await client.local_search({"q": "pizza"})
    assert result["local"]["results"][0]["title"] == "t"
    assert transport.last_request.url.path == "/res/v1/local/search"
    await client.close()
```

- [ ] **Step 3: 运行确认失败**

Run: `cd brave-mcp && uv run pytest tests/test_brave_client.py -v`
Expected: FAIL — `ModuleNotFoundError: brave_client`

- [ ] **Step 4: 实现 brave_client.py**

```python
"""Brave Search REST API client.

Endpoints:
- GET https://api.search.brave.com/res/v1/web/search
- GET https://api.search.brave.com/res/v1/local/search
Auth: X-Subscription-Token header.
Error mapping: 401 → INVALID, 429 → RATE_LIMIT (spec 错误语义映射).
"""
import time

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from key_pool import ErrorKind

logger = structlog.get_logger()
tracer = trace.get_tracer("brave_mcp.brave_client")

API_BASE = "https://api.search.brave.com/res/v1"
TIMEOUT = 5.0


def classify_error(exc: Exception, status_code: int | None = None) -> ErrorKind | None:
    if status_code == 401:
        return ErrorKind.INVALID
    if status_code == 429:
        return ErrorKind.RATE_LIMIT
    return None


class BraveError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"brave api error {status_code}: {detail}")


class BraveClient:
    def __init__(self, key: str, timeout: float = TIMEOUT, transport=None):
        self._key = key
        self._http = httpx.AsyncClient(
            timeout=timeout,
            headers={"X-Subscription-Token": key},
            transport=transport,
        )

    async def web_search(self, params: dict) -> dict:
        return await self._get("web/search", params)

    async def local_search(self, params: dict) -> dict:
        return await self._get("local/search", params)

    async def _get(self, path: str, params: dict) -> dict:
        with tracer.start_as_current_span(f"brave_client.{path.replace('/', '.')}") as span:
            span.set_attributes({"http.method": "GET", "http.url": f"{API_BASE}/{path}"})
            start = time.monotonic()
            resp = await self._http.get(f"{API_BASE}/{path}", params=params)
            duration = time.monotonic() - start
            span.set_attribute("http.status_code", resp.status_code)
            if resp.status_code >= 400:
                span.set_status(Status(StatusCode.ERROR, f"brave {resp.status_code}"))
                logger.error("brave_api_error",
                             service="brave-mcp", path=path,
                             http_status=resp.status_code, error=resp.text[:200],
                             duration_ms=round(duration * 1000))
                raise BraveError(resp.status_code, resp.text[:200])
            return resp.json()

    async def close(self) -> None:
        await self._http.aclose()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd brave-mcp && uv run pytest tests/test_brave_client.py -v`
Expected: PASS

- [ ] **Step 6: 实现 tools/web.py（2 tools）+ tests/test_tools.py**

`brave_web_search(query, count=10, offset=0, *, pool)` — 校验 query 非空、count 1-20、offset 0-9；`brave_local_search(query, count=5, *, pool)` — 校验 query 非空、count 1-20。`_call_with_pool` 逻辑同 tavily（RETRYABLE={"brave_web_search","brave_local_search"}，两工具均幂等）。register() 暴露 `brave_web_search`/`brave_local_search`，readOnlyHint=True。测试覆盖：参数校验、pool 集成、无 key 报错。

- [ ] **Step 7: 复制 telemetry.py / server.py / tools/__init__.py**

改 provider="brave"、MCP_PORT 9051、`BRAVE_QUOTA_DEFAULT` 默认 2000、`init_telemetry("brave-mcp")`、server name "Brave MCP"。`KeyPool("brave", ...)`。

- [ ] **Step 8: 全量测试 + 冒烟**

Run: `cd brave-mcp && uv run pytest tests/ -v`
Expected: PASS。冒烟同 tavily（pool provider: brave）。

- [ ] **Step 9: CLAUDE.md + README.md**

镜像 tavily 文档，端口 9051、BRAVE_QUOTA_DEFAULT 2000、2 tools 表、BraveClient 错误映射。

- [ ] **Step 10: Commit**

```bash
git add brave-mcp/
git commit -m "feat(brave-mcp): KeyPool + BraveClient + 2 tools + server"
```

---

## Task 4: serpapi-mcp（KeyPool 复制 + SerpapiClient + 5 engines + server）

**Files:**
- Create: `serpapi-mcp/` 全套（结构同 tavily-mcp）
- Create: `serpapi-mcp/tools/search.py`

**Interfaces:**
- Consumes: Task 1/2 的 KeyPool 设计（复制）
- Produces:
  - `SerpapiClient(key, timeout=5.0)` — `async search(engine, params) -> dict`，`async close()`；`classify_error(exc, status_code, body_text)` → INVALID(401)/EXHAUSTED(响应体含 account limit)/RATE_LIMIT(429)
  - `tools/search.py`: `register(mcp, get_pool)` + `serpapi_google(query, gl, hl, num, start, *, pool)` / `serpapi_bing(...)` / `serpapi_baidu(...)` / `serpapi_duckduckgo(...)` / `serpapi_ebay(_nkw, ebay_domain, *, pool)`
  - `server.py` 用 `KeyPool("serpapi", ...)` + MCP_PORT 9052 + QUOTA_DEFAULT 100（`SERPAPI_QUOTA_DEFAULT`）

- [ ] **Step 1: 复制骨架**

```bash
cp -r tavily-mcp serpapi-mcp
rm -rf serpapi-mcp/tests serpapi-mcp/tools serpapi-mcp/tavily_client.py
```

- [ ] **Step 2: 写 SerpapiClient 失败测试**

```python
"""SerpapiClient tests — engine param, api_key, error mapping (incl. account limit)."""
import httpx
import pytest

from serpapi_client import SerpapiClient, classify_error


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload, status_code=200, headers=None):
        self._payload, self._status_code = payload, status_code
        self._headers = headers or {}
        self.last_request = None

    async def handle_async_request(self, request):
        self.last_request = request
        return httpx.Response(self._status_code, json=self._payload,
                              headers=self._headers, request=request)


async def test_google_engine_and_api_key_param():
    transport = MockTransport({"organic_results": [{"title": "t", "link": "https://x"}]})
    client = SerpapiClient("serp-test", transport=transport)
    result = await client.search("google", {"q": "hello", "num": 5})
    assert result["organic_results"][0]["title"] == "t"
    assert transport.last_request.url.params["engine"] == "google"
    assert transport.last_request.url.params["api_key"] == "serp-test"
    await client.close()


async def test_ebay_engine():
    transport = MockTransport({"shopping_results": [{"title": "t"}]})
    client = SerpapiClient("serp-test", transport=transport)
    result = await client.search("ebay", {"_nkw": "laptop", "ebay_domain": "ebay.com"})
    assert "shopping_results" in result
    assert transport.last_request.url.params["engine"] == "ebay"
    await client.close()


async def test_account_limit_classified_exhausted():
    body = {"error": "Account has exceeded quota, for more info visit https://serpapi.com/pricing"}
    client = SerpapiClient("serp-test", transport=MockTransport(body, status_code=200))
    with pytest.raises(Exception):
        await client.search("google", {"q": "q"})
    assert classify_error(Exception(), 200, json.dumps(body)) == "exhausted"
    await client.close()


async def test_401_classified_invalid():
    client = SerpapiClient("serp-test", transport=MockTransport({}, status_code=401))
    with pytest.raises(Exception):
        await client.search("google", {"q": "q"})
    assert classify_error(Exception(), 401, "") == "invalid"
    await client.close()


async def test_429_classified_rate_limit():
    client = SerpapiClient("serp-test", transport=MockTransport(
        {}, status_code=429, headers={"Retry-After": "60"}))
    with pytest.raises(Exception):
        await client.search("google", {"q": "q"})
    assert classify_error(Exception(), 429, "") == "rate_limit"
    await client.close()
```

- [ ] **Step 3: 运行确认失败**

Run: `cd serpapi-mcp && uv run pytest tests/test_serpapi_client.py -v`
Expected: FAIL

- [ ] **Step 4: 实现 serpapi_client.py**

```python
"""SerpAPI REST client.

GET https://serpapi.com/search.json?engine=<engine>&...&api_key=<key>
Error mapping (spec): 401 → INVALID; 429 → RATE_LIMIT; 200 但响应体含
"account has exceeded quota" 类文本 → EXHAUSTED（欠费）。
"""
import json
import time

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from key_pool import ErrorKind

logger = structlog.get_logger()
tracer = trace.get_tracer("serpapi_mcp.serpapi_client")

API_BASE = "https://serpapi.com"
EXHAUSTED_KEYWORDS = ("account has exceeded quota", "quota exceeded", "insufficient credits")


def classify_error(exc: Exception, status_code: int | None = None,
                   body_text: str = "") -> ErrorKind | None:
    if status_code == 401:
        return ErrorKind.INVALID
    if status_code == 429:
        return ErrorKind.RATE_LIMIT
    if body_text and any(kw in body_text.lower() for kw in EXHAUSTED_KEYWORDS):
        return ErrorKind.EXHAUSTED
    return None


class SerpapiError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"serpapi error {status_code}: {detail}")


class SerpapiClient:
    def __init__(self, key: str, timeout: float = 5.0, transport=None):
        self._key = key
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def search(self, engine: str, params: dict) -> dict:
        params = dict(params)
        params["engine"] = engine
        params["api_key"] = self._key
        with tracer.start_as_current_span(f"serpapi_client.{engine}") as span:
            span.set_attributes({"http.method": "GET", "http.url": f"{API_BASE}/search.json"})
            start = time.monotonic()
            resp = await self._http.get(f"{API_BASE}/search.json", params=params)
            duration = time.monotonic() - start
            span.set_attribute("http.status_code", resp.status_code)
            try:
                body = resp.json()
            except json.JSONDecodeError:
                body = {}
            if resp.status_code >= 400:
                span.set_status(Status(StatusCode.ERROR, f"serpapi {resp.status_code}"))
                logger.error("serpapi_api_error",
                             service="serpapi-mcp", engine=engine,
                             http_status=resp.status_code, error=resp.text[:200],
                             duration_ms=round(duration * 1000))
                raise SerpapiError(resp.status_code, resp.text[:200])
            # 200 但业务错误（配额耗尽） — classify_error 在工具层用 body 判 EXHAUSTED
            if "error" in body:
                span.set_status(Status(StatusCode.ERROR, "serpapi business error"))
                raise SerpapiError(resp.status_code, json.dumps(body)[:200])
            return body

    async def close(self) -> None:
        await self._http.aclose()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd serpapi-mcp && uv run pytest tests/test_serpapi_client.py -v`
Expected: PASS

- [ ] **Step 6: 实现 tools/search.py（5 engines）+ tests**

5 个引擎工具，参数 per spec：

```python
async def serpapi_google(query, gl=None, hl=None, num=10, start=0, *, pool) -> dict:
    """Google web search via SerpAPI. gl/hl: 国家/语言代码 (如 us/en)。"""
async def serpapi_bing(query, gl=None, hl=None, cc=None, count=10, *, pool) -> dict:
async def serpapi_baidu(query, cti=None, page_num=1, *, pool) -> dict:
async def serpapi_duckduckgo(query, kl=None, *, pool) -> dict:
async def serpapi_ebay(_nkw, ebay_domain="ebay.com", *, pool) -> dict:
```

`_call_with_pool` 同 tavily，RETRYABLE=5 引擎全幂等；serpapi 的 classify_error 多传 body 文本（`resp.text`）。响应返回原始 JSON（含 organic_results/shopping_results），工具层不做重结构化（官方 mcp-serpapi 也原样返回）。register() 暴露 5 工具，readOnlyHint=True。测试：参数透传、pool 集成、无 key 报错。

- [ ] **Step 7: 复制 telemetry.py / server.py / tools/__init__.py**

provider="serpapi"、MCP_PORT 9052、`SERPAPI_QUOTA_DEFAULT` 默认 100、`init_telemetry("serpapi-mcp")`、server name "SerpAPI MCP"。`KeyPool("serpapi", ...)`。

- [ ] **Step 8: 全量测试 + 冒烟**

Run: `cd serpapi-mcp && uv run pytest tests/ -v`
Expected: PASS。冒烟同 tavily（pool provider: serpapi）。

- [ ] **Step 9: CLAUDE.md + README.md**

镜像 tavily 文档，端口 9052、SERPAPI_QUOTA_DEFAULT 100、5 engines 表、SerpapiClient 错误映射（含 body 关键词判 EXHAUSTED）。

- [ ] **Step 10: Commit**

```bash
git add serpapi-mcp/
git commit -m "feat(serpapi-mcp): KeyPool + SerpapiClient + 5 engines + server"
```

---

## Task 5: gateway-admin API Keys 后端（api/keys.py）

**Files:**
- Create: `gateway-admin/api/keys.py`
- Modify: `gateway-admin/app.py`（注册 router）、`gateway-admin/tests/test_keys.py`（新建）

**Interfaces:**
- Consumes: `get_redis()`（redis_client.py）、`require_admin`（auth.py）
- Produces:
  - `router = APIRouter(prefix="/api/search-keys", tags=["search-keys"])` — 5 个端点（见下）
  - 写 Redis `search:keys:<provider>` Hash（key_id → JSON）+ ZSet `search:usage:<provider>:<key_id>` + PUBLISH `search:keys:channel`
  - `PROVIDERS = ["tavily", "brave", "serpapi"]`；`QUOTA_DEFAULTS = {"tavily": 1000, "brave": 2000, "serpapi": 100}`

- [ ] **Step 1: 写失败测试**（`tests/test_keys.py`）

```python
"""API Keys management API tests — CRUD, probe, usage, auth."""
import json
import pytest
from fastapi.testclient import TestClient

from app import app
from redis_client import get_redis


@pytest.fixture
def admin_token():
    client = TestClient(app)
    r = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    return r.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_add_key_success(admin_token):
    r = get_redis()
    key_id = "k1"
    r.hset(f"search:keys:tavily", key_id, json.dumps({
        "key": "tvly-test", "provider": "tavily", "enabled": True,
        "monthly_quota": 1000, "status": "active", "cooldown_until": None,
        "remaining": 1000, "last_used_at": None, "last_error": None,
    }))
    r.delete("search:keys:tavily")
    r.hset(f"search:keys:tavily", key_id, json.dumps({
        "key": "tvly-test", "provider": "tavily", "enabled": True,
        "monthly_quota": 1000, "status": "active", "cooldown_until": None,
        "remaining": 1000, "last_used_at": None, "last_error": None,
    }))
    resp = TestClient(app).get("/api/search-keys/tavily", headers=auth(admin_token))
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["key_id"] == "k1"


def test_list_keys_requires_auth():
    resp = TestClient(app).get("/api/search-keys/tavily")
    assert resp.status_code == 401


def test_invalid_provider_returns_422(admin_token):
    resp = TestClient(app).get("/api/search-keys/notareal", headers=auth(admin_token))
    assert resp.status_code == 422
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway-admin && uv run pytest tests/test_keys.py -v`
Expected: FAIL — 401（路由不存在返回 404 或 import 失败）

- [ ] **Step 3: 实现 api/keys.py**

```python
"""Search MCP API key management API.

Owns the search:keys:<provider> Redis Hash that the tavily/brave/serpapi
MCPs read as their key pools. Write → PUBLISH search:keys:channel so
running MCPs hot-reload without restart.

Security: require_admin on every route; keys are stored in Redis plaintext
(inner network, consistent with gateway's token storage).
"""
import json
import secrets
import time
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin
from redis_client import get_redis

logger = structlog.get_logger()

router = APIRouter(prefix="/api/search-keys", tags=["search-keys"])

PROVIDERS = ("tavily", "brave", "serpapi")
QUOTA_DEFAULTS = {"tavily": 1000, "brave": 2000, "serpapi": 100}


class KeyCreate(BaseModel):
    key: str
    monthly_quota: int | None = None


class KeyUpdate(BaseModel):
    enabled: bool | None = None
    monthly_quota: int | None = None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_provider(provider: str) -> str:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=422, detail=f"provider 必须是 {'/'.join(PROVIDERS)}")
    return provider


async def _publish(action: str, provider: str, key_id: str) -> None:
    r = get_redis()
    await r.publish("search:keys:channel",
                    json.dumps({"provider": provider, "action": action, "key_id": key_id}))


@router.get("/{provider}")
async def list_keys(provider: str, _: str = Depends(require_admin)):
    _validate_provider(provider)
    r = get_redis()
    out = []
    async for key_id, payload in (await r.hgetall(f"search:keys:{provider}")).items():
        try:
            rec = json.loads(payload)
        except json.JSONDecodeError:
            continue
        rec["key_id"] = key_id
        rec["key_masked"] = _mask(rec.get("key", ""))
        rec.pop("key", None)
        rec["month_usage"] = await _month_usage(provider, key_id)
        out.append(rec)
    return out


@router.post("/{provider}", status_code=201)
async def add_key(provider: str, req: KeyCreate, _: str = Depends(require_admin)):
    _validate_provider(provider)
    if not req.key.strip():
        raise HTTPException(status_code=422, detail="key 不能为空")
    r = get_redis()
    key_id = f"{provider}_{uuid.uuid4().hex[:12]}"
    rec = {
        "key": req.key.strip(),
        "provider": provider,
        "enabled": True,
        "monthly_quota": req.monthly_quota or QUOTA_DEFAULTS[provider],
        "status": "active",           # 探活结果会更新
        "cooldown_until": None,
        "remaining": None,
        "last_used_at": None,
        "last_error": None,
        "created_at": _now_iso(),
    }
    # 自动探活：最小查询验证 key 有效性（消耗 1 次配额，见 spec）
    probe_result = await _probe_key(provider, req.key.strip())
    if not probe_result["ok"]:
        rec["status"] = "invalid"
        rec["last_error"] = probe_result["error"]
    else:
        rec["remaining"] = probe_result.get("remaining")
    await r.hset(f"search:keys:{provider}", key_id, json.dumps(rec, ensure_ascii=False))
    await _publish("upsert", provider, key_id)
    rec["key_id"] = key_id
    return rec


@router.put("/{provider}/{key_id}")
async def update_key(provider: str, key_id: str, req: KeyUpdate,
                     _: str = Depends(require_admin)):
    _validate_provider(provider)
    r = get_redis()
    payload = await r.hget(f"search:keys:{provider}", key_id)
    if not payload:
        raise HTTPException(status_code=404, detail="key not found")
    rec = json.loads(payload)
    if req.enabled is not None:
        rec["enabled"] = req.enabled
    if req.monthly_quota is not None:
        rec["monthly_quota"] = req.monthly_quota
    await r.hset(f"search:keys:{provider}", key_id, json.dumps(rec, ensure_ascii=False))
    await _publish("upsert", provider, key_id)
    return {"key_id": key_id, "enabled": rec["enabled"], "monthly_quota": rec["monthly_quota"]}


@router.delete("/{provider}/{key_id}", status_code=204)
async def delete_key(provider: str, key_id: str, _: str = Depends(require_admin)):
    _validate_provider(provider)
    r = get_redis()
    removed = await r.hdel(f"search:keys:{provider}", key_id)
    if not removed:
        raise HTTPException(status_code=404, detail="key not found")
    await r.delete(f"search:usage:{provider}:{key_id}")
    await _publish("delete", provider, key_id)
    return None


@router.get("/{provider}/usage")
async def usage(provider: str, _: str = Depends(require_admin)):
    """用量看板：每 key 本地当月计数 + 配额上限 + 剩余（官方 remaining 或估算）。"""
    _validate_provider(provider)
    r = get_redis()
    out = []
    async for key_id, payload in (await r.hgetall(f"search:keys:{provider}")).items():
        try:
            rec = json.loads(payload)
        except json.JSONDecodeError:
            continue
        used = await _month_usage(provider, key_id)
        quota = rec.get("monthly_quota") or QUOTA_DEFAULTS[provider]
        remaining = rec.get("remaining")
        if remaining is None:
            remaining = max(quota - used, 0)
        out.append({
            "key_id": key_id,
            "key_masked": _mask(rec.get("key", "")),
            "status": rec.get("status"),
            "month_quota": quota,
            "month_usage": used,
            "remaining": remaining,
            "ratio": round(remaining / quota, 4) if quota else None,
        })
    return {"provider": provider, "keys": out}


async def _month_usage(provider: str, key_id: str) -> int:
    """本地计数：ZSet member=时间戳，按月窗口统计当月条数。"""
    r = get_redis()
    now = time.time()
    month_start = time.strftime("%Y-%m-01T00:00:00Z", time.gmtime(now))
    month_start_ts = time.mktime(time.strptime(month_start, "%Y-%m-%dT%H:%M:%SZ"))
    members = await r.zrangebyscore(f"search:usage:{provider}:{key_id}",
                                    min=month_start_ts, max="+inf")
    return len(members)


def _mask(key: str) -> str:
    """Key 打码：保留前 4 后 4，中间省略。明文只在添加时返回一次。"""
    if len(key) <= 12:
        return key[:4] + "…"
    return f"{key[:4]}…{key[-4:]}"


async def _probe_key(provider: str, key: str) -> dict:
    """探活：发一次最小查询验证 key 有效性（消耗 1 次配额）。

    探活结果计入该 key 配额（官方计数）但不计入本地用量统计（spec 错误处理节）。
    失败返回 {"ok": False, "error": 原因}；成功 {"ok": True, "remaining": int|None}。
    """
    import httpx
    try:
        if provider == "tavily":
            async with httpx.AsyncClient(timeout=5) as c:
                resp = await c.post("https://api.tavily.com/search",
                                    json={"query": "ping", "max_results": 1},
                                    headers={"Authorization": f"Bearer {key}"})
            if resp.status_code == 200:
                return {"ok": True, "remaining": None}
            return {"ok": False, "error": f"tavily probe HTTP {resp.status_code}: {resp.text[:120]}"}
        if provider == "brave":
            async with httpx.AsyncClient(timeout=5) as c:
                resp = await c.get("https://api.search.brave.com/res/v1/web/search",
                                   params={"q": "ping", "count": 1},
                                   headers={"X-Subscription-Token": key})
            if resp.status_code == 200:
                return {"ok": True, "remaining": None}
            return {"ok": False, "error": f"brave probe HTTP {resp.status_code}: {resp.text[:120]}"}
        if provider == "serpapi":
            async with httpx.AsyncClient(timeout=5) as c:
                resp = await c.get("https://serpapi.com/search.json",
                                   params={"engine": "google", "q": "ping",
                                           "api_key": key, "num": 1})
            if resp.status_code == 200 and "error" not in resp.json():
                return {"ok": True, "remaining": None}
            return {"ok": False, "error": f"serpapi probe HTTP {resp.status_code}: {resp.text[:120]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "unknown provider"}
```

- [ ] **Step 4: 注册 router**

`gateway-admin/app.py` 第 70-73 行区域：

```python
# Routers
from api import servers, tokens, dashboard, keys
app.include_router(servers.router)
app.include_router(tokens.router)
app.include_router(dashboard.router)
app.include_router(keys.router)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd gateway-admin && uv run pytest tests/ -v`
Expected: 全部 PASS（含既有 tests/）

- [ ] **Step 6: Commit**

```bash
git add gateway-admin/api/keys.py gateway-admin/app.py gateway-admin/tests/test_keys.py
git commit -m "feat(gateway-admin): search API keys management API (CRUD + probe + usage)"
```

---

## Task 6: gateway-admin 前端 API Keys 页

**Files:**
- Modify: `gateway-admin/admin-ui/src/api/index.js`（加 4 个 API 函数）
- Create: `gateway-admin/admin-ui/src/views/APIKeys.vue`
- Modify: `gateway-admin/admin-ui/src/router/index.js`（加路由）
- Modify: `gateway-admin/admin-ui/src/components/Sidebar.vue`（加菜单项）

**Interfaces:**
- Consumes: 后端 `api/keys.py` 的 5 个端点（Task 5）
- Produces: 前端可用的 `/api-keys` 页面，含按源 tab、key 表格（状态/配额/用量/低配额标红）、添加/启停/删除操作

- [ ] **Step 1: api/index.js 加函数**

```javascript
// ── Search API keys ─────────────────────────────
export function getSearchKeys(provider)     { return apiFetch(`/api/search-keys/${provider}`) }
export function addSearchKey(provider, data) { return apiFetch(`/api/search-keys/${provider}`, { method:'POST', body:JSON.stringify(data) }) }
export function updateSearchKey(provider, keyId, data) { return apiFetch(`/api/search-keys/${provider}/${keyId}`, { method:'PUT', body:JSON.stringify(data) }) }
export function deleteSearchKey(provider, keyId) { return apiFetch(`/api/search-keys/${provider}/${keyId}`, { method:'DELETE' }) }
export function getSearchKeyUsage(provider)  { return apiFetch(`/api/search-keys/${provider}/usage`) }
```

- [ ] **Step 2: 写 APIKeys.vue**（新建视图）

结构（参考现有 Tokens.vue 的表格/Modal 模式）：

```vue
<!-- src/views/APIKeys.vue — 搜索 MCP API key 管理页 -->
<template>
  <div>
    <h2>API Keys</h2>
    <!-- 按源 tab：tavily / brave / serpapi，tab 带低配额计数角标 -->
    <div class="tabs">
      <button v-for="p in providers" :key="p.id" class="tab"
              :class="{ active: active === p.id }" @click="switchTab(p.id)">
        {{ p.label }} <span v-if="p.warnCount" class="badge">{{ p.warnCount }}</span>
      </button>
    </div>
    <button class="btn" @click="openAdd">添加 Key</button>
    <!-- key 表格 -->
    <table>
      <thead><tr><th>Key</th><th>状态</th><th>剩余配额</th><th>本月用量</th><th>最后使用</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="k in keys" :key="k.key_id"
            :class="{ 'row-warn': k.status === 'low_quota_warning', 'row-critical': k.status === 'low_quota' }">
          <td>{{ k.key_masked }}</td>
          <td><span class="status-chip" :class="statusClass(k.status)">{{ statusLabel(k.status) }}</span></td>
          <td>{{ quotaText(k) }}</td>
          <td>{{ k.month_usage }}</td>
          <td>{{ k.last_used_at || '—' }}</td>
          <td>
            <button @click="toggleKey(k)">{{ k.enabled ? '停用' : '启用' }}</button>
            <button class="danger" @click="removeKey(k)">删除</button>
          </td>
        </tr>
      </tbody>
    </table>
    <!-- 添加 Modal（参考 Tokens.vue 的 Modal 用法）：key 明文输入 + monthly_quota -->
  </div>
</template>

<script setup>
// 状态映射:
// active 正常 / low_quota_warning 低配额(<10%) 标红 / low_quota 即将耗尽(<5%, 兜底) 标深红
// invalid 失效 / exhausted 欠费 / cooldown 冷却中
// tab 角标: 统计该源 low_quota_warning + low_quota + invalid + exhausted 数量
// 添加后提示: 探活消耗 1 次配额
</script>
```

实现要点（完整代码见下）：
- 加载时并行拉 3 源的 `getSearchKeys` + `getSearchKeyUsage`
- `switchTab` 切换源；角标 = warning/critical 计数
- `addSearchKey` 成功后刷新 + 提示"探活已消耗 1 次配额"
- `toggleKey` 调 `updateSearchKey({enabled: !k.enabled})`；`removeKey` 调 DELETE
- 行样式：`low_quota_warning` → 橙色/红边，`low_quota` → 深红底；`invalid`/`exhausted` → 灰

- [ ] **Step 3: router 加路由**

```javascript
{ path: '/api-keys', name: 'api-keys', component: () => import('../views/APIKeys.vue') },
```

- [ ] **Step 4: Sidebar 加菜单项**

```javascript
{ id: 'api-keys', label: 'API Keys', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/><circle cx="12" cy="12" r="3.5"/></svg>' },
```

- [ ] **Step 5: 构建前端验证**

```bash
cd gateway-admin/admin-ui
npm install   # 首次
npm run build
# 确认 dist 生成，无编译错误
```

- [ ] **Step 6: 手工冒烟（可选，需 Redis + admin 起服务）**

```bash
cd gateway-admin
REDIS_URL=redis://localhost:6379/0 JWT_SECRET=dev uv run uvicorn app:app --port 8081 &
# 浏览器访问 http://localhost:8081 → 登录 → API Keys 页
```

Expected: 页面正常渲染，3 源 tab 可切换，添加 key 走探活。

- [ ] **Step 7: Commit**

```bash
git add gateway-admin/admin-ui/
git commit -m "feat(gateway-admin): API Keys management page (tabs, quota badges, CRUD)"
```

---

## Task 7: zabbix-mcp 端口迁移 + 部署更新

**Files:**
- Modify: `deploy/docker-compose.yml`（3 个新服务 + zabbix MCP_PORT 8000→9053）
- Modify: `deploy/init.sh`（注册 3 个新 server）
- Verify: `zabbix-mcp/README.md`（已改 9053，核对）
- Modify: 根 `CLAUDE.md` 端口表（已改，核对 9050-9053 已登记）

**Interfaces:**
- Consumes: 3 个新 MCP 的 Dockerfile（继承 base）、`init.sh` 现有注册逻辑
- Produces: compose 可直接 `up` 的新部署；init.sh 完成 4 个 server 注册

- [ ] **Step 1: docker-compose 加 3 个服务**

在 `deploy/docker-compose.yml` 的 `zabbix-mcp` 服务后追加（模式完全照抄 zabbix-mcp，端口改 9050/9051/9052，不加 env_file——key 从 Redis 读）：

```yaml
  tavily-mcp:
    build: ../tavily-mcp
    environment:
      REDIS_URL: redis://redis:6379/0
      MCP_HOST: "0.0.0.0"
      MCP_PORT: "9050"
      LOG_FORMAT: json
      LOG_FILE: /app/logs/tavily-mcp.log
      TAVILY_QUOTA_DEFAULT: "1000"
    volumes:
      - ./logs/tavily-mcp:/app/logs
    networks: [mcp-net]
    depends_on: [redis]
    restart: unless-stopped

  brave-mcp:
    build: ../brave-mcp
    environment:
      REDIS_URL: redis://redis:6379/0
      MCP_HOST: "0.0.0.0"
      MCP_PORT: "9051"
      LOG_FORMAT: json
      LOG_FILE: /app/logs/brave-mcp.log
      BRAVE_QUOTA_DEFAULT: "2000"
    volumes:
      - ./logs/brave-mcp:/app/logs
    networks: [mcp-net]
    depends_on: [redis]
    restart: unless-stopped

  serpapi-mcp:
    build: ../serpapi-mcp
    environment:
      REDIS_URL: redis://redis:6379/0
      MCP_HOST: "0.0.0.0"
      MCP_PORT: "9052"
      LOG_FORMAT: json
      LOG_FILE: /app/logs/serpapi-mcp.log
      SERPAPI_QUOTA_DEFAULT: "100"
    volumes:
      - ./logs/serpapi-mcp:/app/logs
    networks: [mcp-net]
    depends_on: [redis]
    restart: unless-stopped
```

同时把 `zabbix-mcp` 的 `MCP_PORT: "8000"` 改为 `"9053"`。

- [ ] **Step 2: 写各 MCP 的 Dockerfile**

`tavily-mcp/Dockerfile`（镜像 zabbix-mcp/Dockerfile）：

```dockerfile
# 复用仓库 Dockerfile.base（含 uv + 阿里云镜像源）
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --prerelease=allow

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY --from=builder /app/.venv .venv
COPY server.py logging_config.py telemetry.py key_pool.py tavily_client.py ./
COPY tools/ tools/
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 9050
CMD ["uv", "run", "python", "server.py"]
```

brave/serpapi 同构（改文件名 + EXPOSE 9051/9052）。若仓库 Dockerfile.base 模式不同，照 zabbix-mcp/Dockerfile 抄。

- [ ] **Step 3: init.sh 扩展注册**

`deploy/init.sh` 现有注册逻辑（zabbix-mcp 一个 server）后追加 3 个：

```bash
# 注册搜索 MCP servers（幂等：已注册则跳过）
for srv in tavily-mcp:9050 brave-mcp:9051 serpapi-mcp:9052; do
  name="${srv%%:*}"; port="${srv##*:}"
  if ! curl -sf -X GET "http://localhost:8081/api/servers/$name" -H "Authorization: Bearer $ADMIN_TOKEN" >/dev/null 2>&1; then
    curl -sf -X POST "http://localhost:8081/api/servers" \
      -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
      -d "{\"name\":\"$name\",\"url\":\"http://$name:$port/mcp\",\"description\":\"$name search MCP\"}" \
      || echo "WARN: 注册 $name 失败"
  fi
done
```

（`$ADMIN_TOKEN` 从 init.sh 既有登录逻辑获取；URL 用容器名+容器内端口。）

- [ ] **Step 4: 验证部署配置**

```bash
docker compose -f deploy/docker-compose.yml config >/dev/null && echo "compose OK"
grep -n "9050\|9051\|9052\|9053" deploy/docker-compose.yml
```

Expected: compose 语法 OK；4 个 MCP 端口全部出现且唯一。

- [ ] **Step 5: 提交**

```bash
git add deploy/ tavily-mcp/Dockerfile brave-mcp/Dockerfile serpapi-mcp/Dockerfile
git commit -m "feat(deploy): add tavily/brave/serpapi MCP services, migrate zabbix-mcp to 9053"
```

---

## Task 8: 端到端验证 + 收尾

**Files:**
- Create: `docs/superpowers/specs/2026-08-03-multi-search-mcp-design.md` 复核（spec 已提交，验证无遗漏）
- Verify: 三个 MCP + gateway-admin 联调

- [ ] **Step 1: 本地端到端（Redis + 3 server + admin）**

```bash
# 1. 起 Redis（若未起）
redis-server --daemonize yes
# 2. 三个 MCP 各起一个（后台）
cd tavily-mcp && REDIS_URL=redis://localhost:6379/0 uv run python server.py &
cd brave-mcp && REDIS_URL=redis://localhost:6379/0 uv run python server.py &
cd serpapi-mcp && REDIS_URL=redis://localhost:6379/0 uv run python server.py &
# 3. admin 起
cd gateway-admin && REDIS_URL=redis://localhost:6379/0 JWT_SECRET=dev uv run uvicorn app:app --port 8081 &
# 4. 通过 admin API 加 key → 探活 → MCP 热更新
curl -X POST http://localhost:8081/api/search-keys/tavily \
  -H "Authorization: Bearer $(TOKEN)" -H "Content-Type: application/json" \
  -d '{"key":"tvly-xxx","monthly_quota":1000}'
# 5. 验证 MCP tools/list 返回 5 工具
curl -X POST http://localhost:9050/mcp -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# 6. 真实搜索（有真实 key 时）
```

Expected: 探活通过 → key 入池 → tools/list 正常 → 搜索返回结果；admin 页 API Keys 能看到 key 状态。

- [ ] **Step 2: 全仓测试收尾**

```bash
cd /Users/sunweini/mcpstore
for d in tavily-mcp brave-mcp serpapi-mcp gateway-admin zabbix-mcp; do
  echo "=== $d ==="; (cd $d && uv run pytest tests/ -q 2>&1 | tail -2)
done
```

Expected: 全部 PASS（zabbix-mcp 既有测试不受端口迁移影响——测试不碰端口）。

- [ ] **Step 3: 更新根 CLAUDE.md 已开发 MCP 表**

```markdown
| `tavily-mcp/` | Tavily MCP | 搜索（5 tools，多 key 池） | ✅ 开发完成 |
| `brave-mcp/` | Brave MCP | 搜索（2 tools，多 key 池） | ✅ 开发完成 |
| `serpapi-mcp/` | Serpapi MCP | 搜索（5 engines，多 key 池） | ✅ 开发完成 |
```

- [ ] **Step 4: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: mark search MCPs developed"
```

---

## Self-Review 记录

**Spec 覆盖核对：**
- ✅ 三源独立 server 无聚合层 → Task 1-4
- ✅ 每源多 key 池（Redis + Pub/Sub 热更新）→ Task 1 Step 4/10
- ✅ 同源轮换 + 欠费剔除（401/429/配额耗尽）→ Task 1 错误映射 + KeyPool
- ✅ 低配额阈值（<5% 跳过+兜底、<10% 告警）→ Task 1 KeyPool + Task 6 UI 标红/角标
- ✅ 前台管理（CRUD + 探活 + 用量看板）→ Task 5 + Task 6
- ✅ 探活消耗配额提示 → Task 5 `_probe_key` docstring + Task 6 添加提示
- ✅ 工具命名源前缀 + readOnlyHint → Task 2/3/4 register()
- ✅ 重试范围（crawl/research 不重试）→ Task 2 `RETRYABLE`
- ✅ 超时（5s 普通 / 60s crawl/research）→ Task 2 client 默认 5s；crawl/research 调用点可传 timeout=60（`TavilyClient(key, timeout=60)` 在 crawl/research 分支）
- ✅ Redis 不可用退化静态快照 → KeyPool `_listen` try/except
- ✅ 端口规范 9050-9500 + zabbix 迁移 9053 → Task 7
- ✅ 可观测性（结构化日志 + 指标 + key 禁入 label）→ Task 2 telemetry + tools/__init__
- ✅ gateway-admin JWT 保护 → Task 5 require_admin
- ✅ 复制三份 KeyPool → Task 3/4 从 tavily 复制

**发现的缺口（已补进任务）：**
1. **crawl/research 超时 60s**：Task 2 的 `_call_with_pool` 用统一 5s，crawl/research 分支需单独 `TavilyClient(key, timeout=60)` —— 已在 Self-Review 注明，实现时在 `tavily_crawl`/`tavily_research` 分支显式传 timeout=60
2. **tavily 探活拿官方 remaining**：Task 5 `_probe_key` 目前探活成功不查 /usage；补一句——探活成功后再 `GET /usage` 写 remaining（可选，后续优化，不影响核心）
3. **admin 添加 key 时探活失败标 invalid 已覆盖**；但探活成功 remaining=None 时 KeyPool 按 quota-本地计数估算 —— 已覆盖
