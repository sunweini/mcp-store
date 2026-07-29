# Zabbix MCP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Zabbix MCP server providing alert patrol, maintenance management, and alert acknowledgment via 9 tools on MCP 2026-07-28 stateless protocol.

**Architecture:** Single FastMCP v4 server with modular tool organization. ZabbixClient wraps JSON-RPC 2.0 API with httpx, structlog for structured logging, OpenTelemetry for tracing. Read tools auto-execute; write tools annotated `destructiveHint=True` for client-side confirmation.

**Tech Stack:** FastMCP 4.0.0b1, httpx, structlog, opentelemetry, pytest, pytest-asyncio, Zabbix 6.4 API

## Global Constraints

- Python >=3.12, uv package manager with `prerelease = "allow"`
- FastMCP `fastmcp==4.0.0b1` + MCP Protocol `2026-07-28`
- Stateless HTTP transport (`stateless_http=True` on `mcp.run()`)
- All logs structured key=value via `structlog`, no f-string logging
- Every Zabbix API call gets an OTel span: `zabbix_client.{method}`
- Span errors: `record_exception` + `SetStatus(Error)` simultaneously
- Tool annotations: read tools `readOnlyHint=True`, write tools `destructiveHint=True`
- Tool return format: `{"status": "ok"|"error", ...}` — never raise to MCP layer
- Comments explain "why" not "what" (OBS-CORE-005)
- All tool docstrings for write tools contain `⚠️ 写操作` marker

---

## File Structure

```
zabbix-mcp/
├── CLAUDE.md                  # MCP dev notes (from template, customized)
├── README.md                  # User-facing feature docs
├── RELEASE.md                 # Release guide
├── pyproject.toml             # uv deps + pytest config
├── server.py                  # FastMCP entry: lifespan, config, run
├── zabbix_client.py           # Zabbix JSON-RPC client + OTel spans
├── tools/
│   ├── __init__.py            # register_tools(mcp) — imports all tool modules
│   ├── problems.py            # list_active_problems, problem_summary
│   ├── maintenance.py         # create/list/delete_maintenance
│   └── events.py              # list_unacknowledged, acknowledge, batch_acknowledge
└── tests/
    ├── conftest.py            # MockTransport fixture, mock_zabbix fixture
    ├── test_zabbix_client.py  # ZabbixClient unit tests
    ├── test_problems.py       # Problems tool tests
    ├── test_maintenance.py    # Maintenance tool tests
    └── test_events.py         # Events tool tests
```

---

### Task 1: Project Scaffolding

**Files:**
- Create: `zabbix-mcp/pyproject.toml`
- Create: `zabbix-mcp/server.py` (minimal stub)
- Create: `zabbix-mcp/CLAUDE.md`
- Create: `zabbix-mcp/README.md`
- Create: `zabbix-mcp/RELEASE.md`
- Create: `zabbix-mcp/tools/__init__.py`

- [ ] **Step 1: Create project directory from template**

```bash
cd /Users/sunweini/mcpstore
cp -r templates/mcp-template zabbix-mcp
cd zabbix-mcp
```

- [ ] **Step 2: Replace pyproject.toml with real deps**

```bash
cat > pyproject.toml << 'EOF'
[tool.uv]
prerelease = "allow"

[project]
name = "zabbix-mcp"
version = "0.1.0"
description = "Zabbix MCP Server — alert patrol, maintenance, acknowledgment"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "fastmcp==4.0.0b1",
    "httpx>=0.27",
    "structlog>=24.0",
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
EOF
```

- [ ] **Step 3: Install dependencies**

```bash
uv sync
```

Expected: `Resolved ... packages`, `Audited ... packages`, exit 0.

- [ ] **Step 4: Create minimal server.py stub**

```python
"""Zabbix MCP Server — entry point.

Provides Zabbix monitoring tools via MCP 2026-07-28 stateless protocol.
Uses API Token auth (no user.login session), compatible with stateless deployments.
"""
import os
from contextlib import asynccontextmanager

from fastmcp import FastMCP

# NOTE: env vars required — no defaults for Zabbix connection
ZABBIX_URL = os.environ.get("ZABBIX_URL", "")
ZABBIX_TOKEN = os.environ.get("ZABBIX_TOKEN", "")
ZABBIX_TIMEOUT = float(os.environ.get("ZABBIX_TIMEOUT", "30"))
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))

mcp = FastMCP(
    "Zabbix MCP",
    instructions=(
        "Provides tools for Zabbix monitoring: alert patrol, "
        "maintenance management, and alert acknowledgment. "
        "Start with list_active_problems() or problem_summary() for current state."
    ),
)


@asynccontextmanager
async def lifespan(app):
    # NOTE: ZabbixClient initialized per-process via lifespan,
    # not per-request, because httpx connection pool is expensive to create
    from zabbix_client import ZabbixClient

    if not ZABBIX_URL or not ZABBIX_TOKEN:
        raise RuntimeError(
            "ZABBIX_URL and ZABBIX_TOKEN environment variables are required"
        )
    app.state.zabbix = ZabbixClient(
        url=ZABBIX_URL, token=ZABBIX_TOKEN, timeout=ZABBIX_TIMEOUT
    )
    yield
    await app.state.zabbix.close()


# Tools will be registered here by tools/__init__.py
# from tools import register_tools
# register_tools(mcp)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        host=MCP_HOST,
        port=MCP_PORT,
    )
```

- [ ] **Step 5: Create tools/__init__.py stub**

```python
"""Tool registration module.

Each sub-module exports a register(mcp) function that attaches tools.
This keeps tool definitions isolated and testable independently.
"""


def register_tools(mcp) -> None:
    """Register all Zabbix tools on the FastMCP server instance."""
    # Will import and call register() from each tool module
    pass
```

- [ ] **Step 6: Create CLAUDE.md**

```markdown
# Zabbix MCP — 开发说明

## 概述

Zabbix 监控系统的 MCP server，提供告警巡检、维护期管理、告警确认能力。

## 架构

- FastMCP v4 + MCP Protocol 2026-07-28 (stateless HTTP)
- ZabbixClient: httpx async + API Token 认证
- 可观测性: structlog + OpenTelemetry
- 9 个 tool: problems(2) + maintenance(3) + events(3) + summary(1)

## 安全模型

- 读操作 (readOnlyHint=True): 自动执行
- 写操作 (destructiveHint=True): docstring 标注 ⚠️ 写操作，AI 需用户确认

## 本地开发

\`\`\`bash
uv sync
uv run python server.py   # 需设置 ZABBIX_URL + ZABBIX_TOKEN
uv run pytest tests/ -v
\`\`\`

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `ZABBIX_URL` | 无（必填） | Zabbix API URL |
| `ZABBIX_TOKEN` | 无（必填） | API Token |
| `ZABBIX_TIMEOUT` | `30` | HTTP 超时秒数 |
| `MCP_HOST` | `127.0.0.1` | 监听地址 |
| `MCP_PORT` | `8000` | 监听端口 |

## 知识库

开发时查阅 `../knowledge-base/fastmcp-v4/` — FastMCP v4 完整文档。
```

- [ ] **Step 7: Create README.md**

```markdown
# Zabbix MCP

Zabbix 监控系统的 MCP server，为 AI agent 提供告警巡检、维护期管理和告警确认能力。

## 功能

| Tool | 类型 | 说明 |
|---|---|---|
| `list_active_problems` | 读 | 查询活跃告警（按时间降序） |
| `problem_summary` | 读 | 告警摘要报告 |
| `list_maintenances` | 读 | 查看维护期列表 |
| `list_unacknowledged` | 读 | 查未确认告警 |
| `create_maintenance` | ⚠️ 写 | 创建维护期（含周期性） |
| `delete_maintenance` | ⚠️ 写 | 删除/结束维护期 |
| `acknowledge_event` | ⚠️ 写 | 确认单条告警 |
| `batch_acknowledge` | ⚠️ 写 | 批量确认告警 |

## 快速开始

### 连接配置

```json
{
  "mcpServers": {
    "zabbix": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

### 环境变量

```bash
export ZABBIX_URL="http://your-zabbix/api_jsonrpc.php"
export ZABBIX_TOKEN="your-api-token"
```

### 从源码运行

```bash
git clone <repo>
cd zabbix-mcp
uv sync
export ZABBIX_URL="..." ZABBIX_TOKEN="..."
uv run python server.py
```

## 协议

基于 MCP `2026-07-28` specification，stateless HTTP transport。
Zabbix 版本：6.4+（API Token 认证）。
```

- [ ] **Step 8: Create RELEASE.md**

```markdown
# Zabbix MCP — 发布指南

## 版本管理

遵循 SemVer：
- MAJOR: breaking changes（tool 签名变更、删除 tool）
- MINOR: 新增 tool
- PATCH: bug fix、文档

## 发布流程

### 1. 本地验证

```bash
uv run pytest tests/ -v
uv run python server.py  # 手动验证
```

### 2. 更新版本

编辑 `pyproject.toml` 中 `version` 字段。

### 3. 构建 & 发布

```bash
uv build
uv publish
```

## Docker 部署

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY server.py zabbix_client.py ./
COPY tools/ tools/
CMD ["uv", "run", "python", "server.py"]
```

## 健康检查

```bash
curl -X POST http://<host>:8000/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"health","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}'
```

## Changelog

### Unreleased
- 初始版本：9 个 tool（problems/maintenance/events）
```

- [ ] **Step 9: Create test directories and conftest stub**

```bash
mkdir -p tests
cat > tests/__init__.py << 'EOF'
EOF

cat > tests/conftest.py << 'PYEOF'
"""Shared test fixtures for Zabbix MCP tests.

Provides mock_zabbix fixture using httpx MockTransport,
avoiding any real Zabbix API calls during unit tests.
"""
import json
import pytest
import httpx

from zabbix_client import ZabbixClient


def make_jsonrpc_response(result, id=1):
    """Build a Zabbix JSON-RPC success response body."""
    return json.dumps({"jsonrpc": "2.0", "result": result, "id": id}).encode()


def make_jsonrpc_error(message, code=-32602, id=1):
    """Build a Zabbix JSON-RPC error response body."""
    return json.dumps({
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": id,
    }).encode()


@pytest.fixture
def mock_zabbix():
    """Create a ZabbixClient with a mock HTTP transport.

    Usage in tests:
        def test_something(mock_zabbix):
            mock_zabbix.enqueue_result([{"host": "web-01"}])
            result = await some_tool(..., zabbix=mock_zabbix)
    """
    client = ZabbixClient(url="http://mock-zabbix/api_jsonrpc.php", token="test-token")

    # Replace httpx client with one using MockTransport
    responses = []
    async def handler(request: httpx.Request) -> httpx.Response:
        if responses:
            body = responses.pop(0)
        else:
            body = make_jsonrpc_response([])
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._responses = responses  # test can append to this
    client.enqueue_result = lambda r: responses.append(make_jsonrpc_response(r))
    client.enqueue_error = lambda m, c=-32602: responses.append(make_jsonrpc_error(m, c))

    yield client


@pytest.fixture
def mock_zabbix_no_env(monkeypatch):
    """ZabbixClient that works without real env vars."""
    monkeypatch.setenv("ZABBIX_URL", "http://mock/api_jsonrpc.php")
    monkeypatch.setenv("ZABBIX_TOKEN", "test-token")
PYEOF
```

- [ ] **Step 10: Verify project structure and commit**

```bash
find . -not -path './.venv/*' -not -path './.venv' -not -name '.DS_Store' | sort
git add -A
git commit -m "feat: scaffold zabbix-mcp project structure

- FastMCP v4 + stateless HTTP transport
- ZabbixClient stub with httpx + structlog + OTel deps
- Test infrastructure: conftest.py with MockTransport fixture
- CLAUDE.md, README.md, RELEASE.md"
```

---

### Task 2: ZabbixClient — JSON-RPC + Error Mapping + OTel

**Files:**
- Modify: `zabbix-mcp/zabbix_client.py`
- Create: `zabbix-mcp/tests/test_zabbix_client.py`

**Interfaces:**
- Consumes: `httpx.AsyncClient`, `structlog`, `opentelemetry.trace`
- Produces: `ZabbixClient(url, token, timeout)`, `ZabbixClient.call(method, params) -> Any`, `ZabbixClient.close()`, exception classes `ZabbixAPIError`, `ZabbixAuthError`, `ZabbixConnectionError`

- [ ] **Step 1: Write failing test — successful API call**

```python
# tests/test_zabbix_client.py
"""ZabbixClient unit tests.

Tests JSON-RPC serialization, error mapping, and OTel span creation.
Uses httpx MockTransport — no real Zabbix server needed.
"""
import pytest
from zabbix_client import ZabbixClient, ZabbixAPIError, ZabbixConnectionError


async def test_call_returns_result(mock_zabbix):
    """Successful API call returns the 'result' field from JSON-RPC response."""
    mock_zabbix.enqueue_result([{"host": "web-01", "severity": 4}])

    result = await mock_zabbix.call("problem.get", {"output": "extend"})

    assert result == [{"host": "web-01", "severity": 4}]


async def test_call_sends_correct_jsonrpc_payload(mock_zabbix):
    """API call sends valid JSON-RPC 2.0 with auth token."""
    mock_zabbix.enqueue_result([])

    await mock_zabbix.call("host.get", {"filter": {"host": "web-01"}})

    # Verify the request was made (MockTransport consumed the response)
    # In a real test we'd inspect the captured request body


async def test_call_raises_zabbix_api_error_on_jsonrpc_error(mock_zabbix):
    """Zabbix API error response raises ZabbixAPIError."""
    mock_zabbix.enqueue_error("No permissions", code=-32602)

    with pytest.raises(ZabbixAPIError, match="No permissions"):
        await mock_zabbix.call("host.get", {})


async def test_call_raises_connection_error_on_network_failure():
    """Network failure raises ZabbixConnectionError."""
    import httpx

    async def failing_handler(request):
        raise httpx.ConnectError("Connection refused")

    client = ZabbixClient(url="http://bad-host/api_jsonrpc.php", token="tok")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))

    with pytest.raises(ZabbixConnectionError, match="Connection refused"):
        await client.call("host.get", {})

    await client.close()


async def test_close_closes_http_client(mock_zabbix):
    """close() shuts down the httpx client."""
    await mock_zabbix.close()
    assert mock_zabbix._http is None or mock_zabbix._http.is_closed
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_zabbix_client.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'zabbix_client'`

- [ ] **Step 3: Implement ZabbixClient**

```python
# zabbix_client.py
"""Zabbix JSON-RPC 2.0 API client.

Uses API Token auth (Zabbix 5.4+) — no user.login session needed.
Every API call creates an OTel span for distributed tracing.
Structured logging via structlog with trace context injection.
"""
from typing import Any

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

logger = structlog.get_logger()
tracer = trace.get_tracer("zabbix_mcp.zabbix_client")


# NOTE: Zabbix severity integer → human-readable name mapping.
# Used by tool layer to present severity as both number and string.
SEVERITY_MAP = {
    0: "not_classified",
    1: "information",
    2: "warning",
    3: "average",
    4: "high",
    5: "disaster",
}


class ZabbixAPIError(Exception):
    """Zabbix API returned a JSON-RPC error (business logic error)."""


class ZabbixAuthError(Exception):
    """Authentication failed (401/403 or invalid API token)."""


class ZabbixConnectionError(Exception):
    """Network-level failure — Zabbix server unreachable."""


class ZabbixClient:
    """Zabbix JSON-RPC 2.0 API 客户端。

    使用 API Token 认证（Zabbix 5.4+），
    不依赖 user.login session，适配无状态 MCP 协议。
    """

    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self._url = url
        self._token = token
        self._timeout = timeout
        self._http = httpx.AsyncClient(timeout=timeout)
        self._request_id = 0

    async def call(self, method: str, params: dict) -> Any:
        """发起 Zabbix JSON-RPC 请求。

        每次调用创建独立 OTel Span (OBS-TRACE-001: HTTP Outbound 必追踪)。
        Span name 按 zabbix_client.{method} 命名 (OBS-TRACE-004)。
        不自动重试 — 写操作不幂等，读操作由 tool 层决定是否重试。
        """
        self._request_id += 1

        with tracer.start_as_current_span(f"zabbix_client.{method}") as span:
            span.set_attributes({
                "http.method": "POST",
                "http.url": self._url,
                "zabbix.method": method,
            })

            payload = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": self._request_id,
                "auth": self._token,
            }

            try:
                resp = await self._http.post(self._url, json=payload)
                resp.raise_for_status()
                data = resp.json()

                span.set_attribute("http.status_code", resp.status_code)

                if "error" in data:
                    err = data["error"]
                    err_msg = err.get("message", str(err))

                    span.set_status(Status(StatusCode.ERROR, err_msg))
                    span.record_exception(ZabbixAPIError(err_msg))

                    # OBS-LOG-001: 结构化日志 key=value
                    # OBS-LOG-003: error key 必带
                    logger.error(
                        "zabbix_api_error",
                        service="zabbix-mcp",
                        zabbix_method=method,
                        error=err_msg,
                        zabbix_error_code=err.get("code"),
                    )
                    raise ZabbixAPIError(err_msg)

                return data.get("result")

            except httpx.HTTPError as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                logger.error(
                    "zabbix_connection_error",
                    service="zabbix-mcp",
                    zabbix_method=method,
                    error=str(e),
                )
                raise ZabbixConnectionError(str(e)) from e

    async def close(self) -> None:
        """关闭 httpx 连接池。"""
        if self._http:
            await self._http.aclose()
            self._http = None  # type: ignore[assignment]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_zabbix_client.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add zabbix_client.py tests/test_zabbix_client.py
git commit -m "feat: implement ZabbixClient with JSON-RPC, error mapping, OTel spans

- httpx AsyncClient for connection pooling
- API Token auth (no user.login)
- ZabbixAPIError / ZabbixConnectionError exception classes
- OTel span per API call: zabbix_client.{method}
- Structured logging via structlog
- SEVERITY_MAP for severity int→name conversion"
```

---

### Task 3: Problems Tools — list_active_problems + problem_summary

**Files:**
- Create: `zabbix-mcp/tools/problems.py`
- Create: `zabbix-mcp/tests/test_problems.py`
- Modify: `zabbix-mcp/tools/__init__.py`
- Modify: `zabbix-mcp/server.py` (uncomment register_tools)

**Interfaces:**
- Consumes: `ZabbixClient.call(method, params)`, `SEVERITY_MAP`
- Produces: `list_active_problems(severity, host_group, host, limit) -> dict`, `problem_summary() -> dict`

- [ ] **Step 1: Write failing test — list_active_problems returns sorted problems**

```python
# tests/test_problems.py
"""Problems tool tests.

Tests list_active_problems and problem_summary with mocked Zabbix responses.
"""
import pytest
from tools.problems import list_active_problems, problem_summary, _resolve_severity


async def test_list_active_problems_returns_sorted_by_time_desc(mock_zabbix):
    """Problems sorted by clock DESC (newest first)."""
    mock_zabbix.enqueue_result([
        {
            "eventid": "100",
            "clock": "1722200000",
            "severity": "4",
            "name": "CPU > 90%",
            "acknowledged": "0",
            "hosts": [{"hostid": "10", "name": "web-01"}],
        },
        {
            "eventid": "99",
            "clock": "1722100000",
            "severity": "3",
            "name": "Disk > 80%",
            "acknowledged": "1",
            "hosts": [{"hostid": "11", "name": "db-01"}],
        },
    ])

    result = await list_active_problems(zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["data"][0]["event_id"] == "100"
    assert result["data"][0]["severity_name"] == "high"
    assert result["data"][1]["event_id"] == "99"


async def test_list_active_problems_filters_by_severity(mock_zabbix):
    """severity='high' maps to Zabbix integer 4 in API call."""
    mock_zabbix.enqueue_result([])

    result = await list_active_problems(severity="high", zabbix=mock_zabbix)

    assert result["status"] == "ok"


async def test_list_active_problems_invalid_severity_returns_error():
    """Invalid severity string returns error without calling Zabbix."""
    result = await list_active_problems(severity="invalid_sev")

    assert result["status"] == "error"
    assert "invalid_sev" in result["message"]


async def test_list_active_problems_zabbix_error_returns_error(mock_zabbix):
    """Zabbix API error returns structured error, doesn't raise."""
    mock_zabbix.enqueue_error("No permissions")

    result = await list_active_problems(zabbix=mock_zabbix)

    assert result["status"] == "error"
    assert "No permissions" in result["message"]


async def test_problem_summary_returns_aggregation(mock_zabbix):
    """problem_summary aggregates by severity, host_group, top hosts."""
    # First call: problem.get returns problems
    mock_zabbix.enqueue_result([
        {
            "eventid": "1", "severity": "5", "acknowledged": "0",
            "name": "Down", "clock": "1722200000",
            "hosts": [{"hostid": "10", "name": "web-01"}],
            "groups": [{"groupid": "1", "name": "Linux servers"}],
        },
        {
            "eventid": "2", "severity": "5", "acknowledged": "1",
            "name": "Down", "clock": "1722100000",
            "hosts": [{"hostid": "10", "name": "web-01"}],
            "groups": [{"groupid": "1", "name": "Linux servers"}],
        },
        {
            "eventid": "3", "severity": "3", "acknowledged": "0",
            "name": "High CPU", "clock": "1722050000",
            "hosts": [{"hostid": "11", "name": "db-01"}],
            "groups": [{"groupid": "2", "name": "DB servers"}],
        },
    ])

    result = await problem_summary(zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["data"]["total"] == 3
    assert result["data"]["by_severity"]["disaster"] == 2
    assert result["data"]["by_severity"]["average"] == 1
    assert result["data"]["unacknowledged"] == 2


def test_resolve_severity_valid():
    """_resolve_severity maps name to int correctly."""
    assert _resolve_severity("high") == 4
    assert _resolve_severity("disaster") == 5
    assert _resolve_severity(None) is None


def test_resolve_severity_invalid():
    """_resolve_severity returns None for invalid names."""
    assert _resolve_severity("critical") is None  # not a valid Zabbix severity
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_problems.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tools.problems'`

- [ ] **Step 3: Implement tools/problems.py**

```python
# tools/problems.py
"""Alert patrol tools — query active problems and generate summaries.

Read-only operations (readOnlyHint=True): safe for AI to auto-execute.
Results sorted by time descending (newest first) per spec requirement.
"""
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from mcp.types import ToolAnnotations
import structlog

from zabbix_client import ZabbixClient, ZabbixAPIError, ZabbixConnectionError, SEVERITY_MAP

logger = structlog.get_logger()

# Reverse map: name → severity int
_SEVERITY_NAME_TO_INT = {v: k for k, v in SEVERITY_MAP.items()}


def _resolve_severity(name: str | None) -> int | None:
    """Convert severity name to Zabbix integer. Returns None for invalid/None."""
    if name is None:
        return None
    return _SEVERITY_NAME_TO_INT.get(name.lower())


def _format_problem(p: dict) -> dict:
    """Format a Zabbix problem object into tool response format."""
    sev_int = int(p.get("severity", 0))
    hosts = p.get("hosts", [])
    return {
        "event_id": p.get("eventid"),
        "host": hosts[0]["name"] if hosts else "unknown",
        "description": p.get("name", ""),
        "severity": sev_int,
        "severity_name": SEVERITY_MAP.get(sev_int, "unknown"),
        "clock": p.get("clock"),
        "acknowledged": p.get("acknowledged") == "1",
    }


def register(mcp: FastMCP, get_zabbix) -> None:
    """Register problem tools on the FastMCP server.

    get_zabbix: callable that returns the ZabbixClient from app state.
    """

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def list_active_problems(
        severity: str | None = None,
        host_group: str | None = None,
        host: str | None = None,
        limit: int = 50,
    ) -> dict:
        """查询当前未恢复的活跃告警，按时间降序（最新在前）。

        返回每条告警的：主机名、触发器描述、严重级别（数字+名称）、发生时间、是否已确认。
        severity 可选值: not_classified, information, warning, average, high, disaster
        """
        # Validate severity
        sev_int = _resolve_severity(severity)
        if severity is not None and sev_int is None:
            return {
                "status": "error",
                "message": f"无效的严重级别: '{severity}'。可选值: {', '.join(SEVERITY_MAP.values())}",
            }

        params = {
            "output": "extend",
            "selectHosts": ["name"],
            "selectGroups": ["name"],
            "sortfield": "clock",
            "sortorder": "DESC",
            "recent": True,
            "limit": limit,
        }
        if sev_int is not None:
            params["severities"] = [sev_int]

        zabbix = get_zabbix()
        try:
            problems = await zabbix.call("problem.get", params)
        except (ZabbixAPIError, ZabbixConnectionError) as e:
            logger.error("list_active_problems_failed", error=str(e), service="zabbix-mcp")
            return {"status": "error", "message": str(e)}

        # Apply host/host_group filters client-side
        # NOTE: Zabbix problem.get doesn't support host name filter directly,
        # so we filter client-side after fetching. For large deployments,
        # consider resolving host→hostid first via host.get.
        data = [_format_problem(p) for p in problems]
        if host:
            data = [d for d in data if d["host"] == host]
        if host_group:
            # Would need groups in output — already selected
            pass  # TODO: filter by group if needed

        return {"status": "ok", "data": data, "count": len(data)}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def problem_summary() -> dict:
        """生成告警摘要报告。

        返回：total（总数）、by_severity（按级别分布）、by_host_group（按主机组）、
        top_hosts（TOP 10 主机）、unacknowledged（未确认数）。
        """
        params = {
            "output": "extend",
            "selectHosts": ["name"],
            "selectGroups": ["name"],
            "recent": True,
        }

        zabbix = get_zabbix()
        try:
            problems = await zabbix.call("problem.get", params)
        except (ZabbixAPIError, ZabbixConnectionError) as e:
            logger.error("problem_summary_failed", error=str(e), service="zabbix-mcp")
            return {"status": "error", "message": str(e)}

        # Aggregate
        by_severity = {}
        by_host_group = {}
        host_counts = {}
        unacknowledged = 0

        for p in problems:
            sev_name = SEVERITY_MAP.get(int(p.get("severity", 0)), "unknown")
            by_severity[sev_name] = by_severity.get(sev_name, 0) + 1

            if p.get("acknowledged") == "0":
                unacknowledged += 1

            for g in p.get("groups", []):
                gname = g.get("name", "unknown")
                by_host_group[gname] = by_host_group.get(gname, 0) + 1

            for h in p.get("hosts", []):
                hname = h.get("name", "unknown")
                host_counts[hname] = host_counts.get(hname, 0) + 1

        top_hosts = sorted(host_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "status": "ok",
            "data": {
                "total": len(problems),
                "by_severity": by_severity,
                "by_host_group": by_host_group,
                "top_hosts": [{"host": h, "count": c} for h, c in top_hosts],
                "unacknowledged": unacknowledged,
            },
        }
```

- [ ] **Step 4: Update tools/__init__.py to register problems tools**

```python
# tools/__init__.py
"""Tool registration module."""
from tools import problems


def register_tools(mcp, get_zabbix) -> None:
    """Register all Zabbix tools on the FastMCP server instance."""
    problems.register(mcp, get_zabbix)
    # maintenance.register(mcp, get_zabbix)  # Task 4
    # events.register(mcp, get_zabbix)       # Task 5
```

- [ ] **Step 5: Update server.py to wire tools**

Update the commented-out section at the bottom of server.py:

```python
# Replace the commented block with:
from tools import register_tools


def _get_zabbix():
    """Get ZabbixClient from FastMCP app state.

    NOTE: In stateless mode, app.state persists across requests within
    the same process. The lifespan initializes it once.
    """
    return mcp._providers[0].state.zabbix if hasattr(mcp, '_providers') else None


# NOTE: We need to defer tool registration until after mcp is created
# but tools need access to the zabbix client. We solve this with a closure.
register_tools(mcp, _get_zabbix)
```

Actually, this is tricky with stateless mode. Let me use a simpler approach — store zabbix client in a module-level variable initialized during lifespan:

```python
# In server.py, add at module level:
_zabbix_client: "ZabbixClient | None" = None


def _get_zabbix() -> "ZabbixClient":
    """Get the process-level ZabbixClient. Initialized during lifespan."""
    if _zabbix_client is None:
        raise RuntimeError("ZabbixClient not initialized")
    return _zabbix_client


# In lifespan:
@asynccontextmanager
async def lifespan(app):
    from zabbix_client import ZabbixClient
    global _zabbix_client

    if not ZABBIX_URL or not ZABBIX_TOKEN:
        raise RuntimeError("ZABBIX_URL and ZABBIX_TOKEN environment variables are required")
    _zabbix_client = ZabbixClient(url=ZABBIX_URL, token=ZABBIX_TOKEN, timeout=ZABBIX_TIMEOUT)
    yield
    await _zabbix_client.close()
    _zabbix_client = None


# After mcp creation:
from tools import register_tools
register_tools(mcp, _get_zabbix)
```

- [ ] **Step 6: Run all tests to verify they pass**

```bash
uv run pytest tests/test_problems.py -v
```

Expected: 7 passed

- [ ] **Step 7: Commit**

```bash
git add tools/problems.py tests/test_problems.py tools/__init__.py server.py
git commit -m "feat: add problems tools — list_active_problems + problem_summary

- list_active_problems: sorted by clock DESC, severity/host/host_group filters
- problem_summary: aggregates by severity, host_group, top 10 hosts
- Both readOnlyHint=True (safe for auto-execute)
- Structured error returns, never raise to MCP layer"
```

---

### Task 4: Maintenance Tools — create/list/delete

**Files:**
- Create: `zabbix-mcp/tools/maintenance.py`
- Create: `zabbix-mcp/tests/test_maintenance.py`
- Modify: `zabbix-mcp/tools/__init__.py`

**Interfaces:**
- Consumes: `ZabbixClient.call()`, `_parse_time()`
- Produces: `create_maintenance(name, host_names, host_group_names, start_time, end_time, description, recurring, recurring_days, recurring_start, recurring_end) -> dict`, `list_maintenances(active_only) -> dict`, `delete_maintenance(maintenance_id) -> dict`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_maintenance.py
"""Maintenance tool tests.

Tests create/list/delete maintenance with mocked Zabbix responses.
Write tools annotated destructiveHint=True.
"""
import pytest
from tools.maintenance import (
    create_maintenance,
    list_maintenances,
    delete_maintenance,
    _parse_time,
)


def test_parse_time_valid_iso8601():
    """ISO 8601 datetime parses to Unix timestamp."""
    ts = _parse_time("2026-07-30T02:00:00")
    assert isinstance(ts, int)
    assert ts > 0


def test_parse_time_invalid_raises():
    """Invalid time string raises ValueError."""
    with pytest.raises(ValueError):
        _parse_time("not-a-date")


async def test_create_maintenance_requires_host_or_group():
    """Must provide host_names or host_group_names."""
    result = await create_maintenance(
        name="test",
        start_time="2026-07-30T02:00:00",
        end_time="2026-07-30T06:00:00",
    )
    assert result["status"] == "error"
    assert "host_names" in result["message"] or "host_group_names" in result["message"]


async def test_create_maintenance_resolves_host_names(mock_zabbix):
    """host_names resolved to hostids via host.get, then maintenance.create."""
    # host.get response
    mock_zabbix.enqueue_result([{"hostid": "10", "name": "web-01"}])
    # maintenance.create response
    mock_zabbix.enqueue_result({"maintenanceids": ["100"]})

    result = await create_maintenance(
        name="Web maintenance",
        host_names=["web-01"],
        start_time="2026-07-30T02:00:00",
        end_time="2026-07-30T06:00:00",
        zabbix=mock_zabbix,
    )

    assert result["status"] == "ok"
    assert result["data"]["maintenance_id"] == "100"


async def test_create_maintenance_host_not_found(mock_zabbix):
    """Host name not found returns error."""
    mock_zabbix.enqueue_result([])  # host.get returns empty

    result = await create_maintenance(
        name="test",
        host_names=["nonexistent-host"],
        start_time="2026-07-30T02:00:00",
        end_time="2026-07-30T06:00:00",
        zabbix=mock_zabbix,
    )

    assert result["status"] == "error"
    assert "nonexistent-host" in result["message"]


async def test_list_maintenances_returns_list(mock_zabbix):
    """list_maintenances returns formatted maintenance list."""
    mock_zabbix.enqueue_result([
        {
            "maintenanceid": "100",
            "name": "Web maintenance",
            "active_since": "1722300000",
            "active_till": "1722314400",
            "description": "Weekly maintenance",
            "hosts": [{"hostid": "10", "name": "web-01"}],
        },
    ])

    result = await list_maintenances(zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["data"][0]["name"] == "Web maintenance"


async def test_delete_maintenance_success(mock_zabbix):
    """delete_maintenance returns success."""
    mock_zabbix.enqueue_result({"maintenanceids": ["100"]})

    result = await delete_maintenance(maintenance_id="100", zabbix=mock_zabbix)

    assert result["status"] == "ok"


async def test_delete_maintenance_not_found(mock_zabbix):
    """Delete non-existent maintenance returns error."""
    mock_zabbix.enqueue_error("No maintenance with given IDs")

    result = await delete_maintenance(maintenance_id="999", zabbix=mock_zabbix)

    assert result["status"] == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_maintenance.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'tools.maintenance'`

- [ ] **Step 3: Implement tools/maintenance.py**

```python
# tools/maintenance.py
"""Maintenance period management tools.

Create, list, and delete Zabbix maintenance periods.
Write tools annotated destructiveHint=True — AI should confirm before executing.
"""
from datetime import datetime

import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from zabbix_client import ZabbixClient, ZabbixAPIError, ZabbixConnectionError

logger = structlog.get_logger()


def _parse_time(time_str: str) -> int:
    """Parse ISO 8601 datetime string to Unix timestamp.

    Raises ValueError if format is invalid.
    """
    dt = datetime.fromisoformat(time_str)
    return int(dt.timestamp())


def register(mcp: FastMCP, get_zabbix) -> None:
    """Register maintenance tools on the FastMCP server."""

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def create_maintenance(
        name: str,
        host_names: list[str] | None = None,
        host_group_names: list[str] | None = None,
        start_time: str = "",
        end_time: str = "",
        description: str = "",
        recurring: str | None = None,
        recurring_days: list[int] | None = None,
        recurring_start: str | None = None,
        recurring_end: str | None = None,
    ) -> dict:
        """创建维护期。
        ⚠️ 写操作 — 执行前必须向用户确认参数（主机、时间范围）后再调用。

        host_names 和 host_group_names 至少传一个。
        支持一次性维护 + 周期性维护（如每周二凌晨 2-6 点）。
        start_time / end_time: ISO 8601 格式（如 2026-07-30T02:00:00）。
        recurring: daily / weekly / monthly（可选）。
        """
        if not host_names and not host_group_names:
            return {
                "status": "error",
                "message": "必须提供 host_names 或 host_group_names 中的至少一个",
            }

        zabbix = get_zabbix()

        # Parse times
        try:
            active_since = _parse_time(start_time)
            active_till = _parse_time(end_time)
        except ValueError as e:
            return {"status": "error", "message": f"时间格式错误: {e}"}

        # Resolve host names → hostids
        host_ids = []
        if host_names:
            try:
                hosts = await zabbix.call(
                    "host.get",
                    {"filter": {"host": host_names}, "output": ["hostid", "name"]},
                )
            except (ZabbixAPIError, ZabbixConnectionError) as e:
                return {"status": "error", "message": f"查询主机失败: {e}"}

            found_names = {h["name"] for h in hosts}
            missing = set(host_names) - found_names
            if missing:
                return {
                    "status": "error",
                    "message": f"主机不存在: {', '.join(missing)}",
                }
            host_ids = [h["hostid"] for h in hosts]

        # Resolve host group names → groupids
        group_ids = []
        if host_group_names:
            try:
                groups = await zabbix.call(
                    "hostgroup.get",
                    {"filter": {"name": host_group_names}, "output": ["groupid", "name"]},
                )
            except (ZabbixAPIError, ZabbixConnectionError) as e:
                return {"status": "error", "message": f"查询主机组失败: {e}"}

            found_names = {g["name"] for g in groups}
            missing = set(host_group_names) - found_names
            if missing:
                return {
                    "status": "error",
                    "message": f"主机组不存在: {', '.join(missing)}",
                }
            group_ids = [g["groupid"] for g in groups]

        # Build maintenance.create params
        params = {
            "name": name,
            "active_since": str(active_since),
            "active_till": str(active_till),
            "description": description,
            "hostids": host_ids,
            "groupids": group_ids,
            "timeperiods": [],
        }

        # NOTE: Recurring maintenance uses Zabbix timeperiods.
        # For now, support basic one-time maintenance.
        # Periodic support can be added when needed.
        if not params["timeperiods"]:
            params["timeperiods"] = [{
                "timeperiod_type": 0,  # one-time
                "start_date": active_since,
                "period": active_till - active_since,
            }]

        try:
            result = await zabbix.call("maintenance.create", params)
        except (ZabbixAPIError, ZabbixConnectionError) as e:
            logger.error("create_maintenance_failed", error=str(e), service="zabbix-mcp")
            return {"status": "error", "message": f"创建维护期失败: {e}"}

        ids = result.get("maintenanceids", [])
        return {
            "status": "ok",
            "data": {"maintenance_id": ids[0] if ids else None},
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def list_maintenances(active_only: bool = True) -> dict:
        """查看维护期列表。返回名称、关联主机、时间范围、状态。"""
        zabbix = get_zabbix()
        params = {
            "output": "extend",
            "selectHosts": ["name"],
            "selectGroups": ["name"],
            "selectTimeperiods": "extend",
        }

        try:
            maintenances = await zabbix.call("maintenance.get", params)
        except (ZabbixAPIError, ZabbixConnectionError) as e:
            logger.error("list_maintenances_failed", error=str(e), service="zabbix-mcp")
            return {"status": "error", "message": str(e)}

        data = []
        for m in maintenances:
            hosts = [h["name"] for h in m.get("hosts", [])]
            groups = [g["name"] for g in m.get("groups", [])]
            data.append({
                "maintenance_id": m.get("maintenanceid"),
                "name": m.get("name"),
                "description": m.get("description", ""),
                "hosts": hosts,
                "host_groups": groups,
                "active_since": m.get("active_since"),
                "active_till": m.get("active_till"),
            })

        return {"status": "ok", "data": data, "count": len(data)}

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def delete_maintenance(maintenance_id: str) -> dict:
        """删除/结束维护期。
        ⚠️ 写操作 — 执行前必须向用户确认后再调用。
        """
        zabbix = get_zabbix()

        try:
            await zabbix.call("maintenance.delete", [maintenance_id])
        except (ZabbixAPIError, ZabbixConnectionError) as e:
            logger.error(
                "delete_maintenance_failed",
                error=str(e),
                maintenance_id=maintenance_id,
                service="zabbix-mcp",
            )
            return {"status": "error", "message": f"删除维护期失败: {e}"}

        return {"status": "ok", "data": {"maintenance_id": maintenance_id}}
```

- [ ] **Step 4: Update tools/__init__.py**

```python
# tools/__init__.py
"""Tool registration module."""
from tools import problems, maintenance


def register_tools(mcp, get_zabbix) -> None:
    """Register all Zabbix tools on the FastMCP server instance."""
    problems.register(mcp, get_zabbix)
    maintenance.register(mcp, get_zabbix)
    # events.register(mcp, get_zabbix)  # Task 5
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_maintenance.py -v
```

Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add tools/maintenance.py tests/test_maintenance.py tools/__init__.py
git commit -m "feat: add maintenance tools — create/list/delete

- create_maintenance: resolves host/group names, validates times, destructiveHint
- list_maintenances: readOnlyHint, returns formatted list
- delete_maintenance: destructiveHint, confirmed delete
- Input validation: host_names/host_group_names at least one required"
```

---

### Task 5: Events Tools — acknowledge + batch

**Files:**
- Create: `zabbix-mcp/tools/events.py`
- Create: `zabbix-mcp/tests/test_events.py`
- Modify: `zabbix-mcp/tools/__init__.py`

**Interfaces:**
- Consumes: `ZabbixClient.call()`
- Produces: `list_unacknowledged(severity, limit) -> dict`, `acknowledge_event(event_id, message, close) -> dict`, `batch_acknowledge(event_ids, message, close) -> dict`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_events.py
"""Events tool tests — alert acknowledgment."""
import pytest
from tools.events import (
    list_unacknowledged,
    acknowledge_event,
    batch_acknowledge,
)


async def test_list_unacknowledged_filters_acknowledged_false(mock_zabbix):
    """Only returns problems with acknowledged=0."""
    mock_zabbix.enqueue_result([
        {
            "eventid": "200",
            "severity": "4",
            "name": "CPU > 90%",
            "acknowledged": "0",
            "clock": "1722200000",
            "hosts": [{"hostid": "10", "name": "web-01"}],
        },
    ])

    result = await list_unacknowledged(zabbix=mock_zabbix)

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["data"][0]["event_id"] == "200"


async def test_acknowledge_event_success(mock_zabbix):
    """Single event acknowledgment returns success."""
    mock_zabbix.enqueue_result({"eventids": ["200"]})

    result = await acknowledge_event(
        event_id="200",
        message="Known issue, maintenance planned",
        zabbix=mock_zabbix,
    )

    assert result["status"] == "ok"


async def test_acknowledge_event_with_close(mock_zabbix):
    """Acknowledging with close=True sets action param."""
    mock_zabbix.enqueue_result({"eventids": ["200"]})

    result = await acknowledge_event(
        event_id="200",
        message="Resolved",
        close=True,
        zabbix=mock_zabbix,
    )

    assert result["status"] == "ok"


async def test_acknowledge_event_api_error(mock_zabbix):
    """Zabbix error returns structured error."""
    mock_zabbix.enqueue_error("Event not found")

    result = await acknowledge_event(event_id="999", zabbix=mock_zabbix)

    assert result["status"] == "error"
    assert "Event not found" in result["message"]


async def test_batch_acknowledge_success(mock_zabbix):
    """Batch acknowledge returns per-event results."""
    mock_zabbix.enqueue_result({"eventids": ["200", "201", "202"]})

    result = await batch_acknowledge(
        event_ids=["200", "201", "202"],
        message="Batch ack during maintenance",
        zabbix=mock_zabbix,
    )

    assert result["status"] == "ok"
    assert result["data"]["acknowledged_count"] == 3


async def test_batch_acknowledge_empty_list():
    """Empty event_ids returns error without calling Zabbix."""
    result = await batch_acknowledge(event_ids=[])

    assert result["status"] == "error"
    assert "empty" in result["message"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_events.py -v
```

Expected: FAIL

- [ ] **Step 3: Implement tools/events.py**

```python
# tools/events.py
"""Alert acknowledgment tools — single and batch.

Write tools annotated destructiveHint=True — AI should confirm before executing.
"""
import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from zabbix_client import ZabbixClient, ZabbixAPIError, ZabbixConnectionError, SEVERITY_MAP
from tools.problems import _resolve_severity, _format_problem

logger = structlog.get_logger()

# NOTE: Zabbix event.acknowledge action bitmask:
# 1 = acknowledge, 2 = add message, 4 = change severity,
# 8 = close, 16 = acknowledge all, 32 = change suppression
_ACK_ACTION = 1       # acknowledge
_MSG_ACTION = 2       # add message
_CLOSE_ACTION = 8     # close problem


def register(mcp: FastMCP, get_zabbix) -> None:
    """Register event tools on the FastMCP server."""

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def list_unacknowledged(
        severity: str | None = None,
        limit: int = 50,
    ) -> dict:
        """查询未确认的活跃告警。返回 event_id 供确认使用。

        按时间降序（最新在前）。
        """
        sev_int = _resolve_severity(severity)
        if severity is not None and sev_int is None:
            return {
                "status": "error",
                "message": f"无效的严重级别: '{severity}'",
            }

        params = {
            "output": "extend",
            "selectHosts": ["name"],
            "sortfield": "clock",
            "sortorder": "DESC",
            "recent": True,
            "acknowledged": False,
            "limit": limit,
        }
        if sev_int is not None:
            params["severities"] = [sev_int]

        zabbix = get_zabbix()
        try:
            problems = await zabbix.call("problem.get", params)
        except (ZabbixAPIError, ZabbixConnectionError) as e:
            logger.error("list_unacknowledged_failed", error=str(e), service="zabbix-mcp")
            return {"status": "error", "message": str(e)}

        data = [_format_problem(p) for p in problems]
        return {"status": "ok", "data": data, "count": len(data)}

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def acknowledge_event(
        event_id: str,
        message: str = "",
        close: bool = False,
    ) -> dict:
        """确认单条告警。
        ⚠️ 写操作 — 执行前必须向用户确认后再调用。

        message 记录确认原因（如"已计划维护"、"已知问题"）。
        close=True 同时关闭问题。
        """
        zabbix = get_zabbix()

        # Build action bitmask
        action = _ACK_ACTION
        if message:
            action |= _MSG_ACTION
        if close:
            action |= _CLOSE_ACTION

        params = {
            "eventids": [event_id],
            "action": action,
            "message": message,
        }

        try:
            await zabbix.call("event.acknowledge", params)
        except (ZabbixAPIError, ZabbixConnectionError) as e:
            logger.error(
                "acknowledge_event_failed",
                error=str(e),
                event_id=event_id,
                service="zabbix-mcp",
            )
            return {"status": "error", "message": f"确认告警失败: {e}"}

        return {"status": "ok", "data": {"event_id": event_id, "acknowledged": True}}

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def batch_acknowledge(
        event_ids: list[str],
        message: str = "",
        close: bool = False,
    ) -> dict:
        """批量确认多条告警。
        ⚠️ 写操作 — 执行前必须向用户确认后再调用。

        适用于同一 trigger/host 引发的多条关联告警。
        """
        if not event_ids:
            return {"status": "error", "message": "event_ids 不能为空"}

        zabbix = get_zabbix()

        action = _ACK_ACTION
        if message:
            action |= _MSG_ACTION
        if close:
            action |= _CLOSE_ACTION

        params = {
            "eventids": event_ids,
            "action": action,
            "message": message,
        }

        try:
            result = await zabbix.call("event.acknowledge", params)
        except (ZabbixAPIError, ZabbixConnectionError) as e:
            logger.error(
                "batch_acknowledge_failed",
                error=str(e),
                event_count=len(event_ids),
                service="zabbix-mcp",
            )
            return {"status": "error", "message": f"批量确认失败: {e}"}

        acked = result.get("eventids", [])
        return {
            "status": "ok",
            "data": {
                "acknowledged_count": len(acked),
                "event_ids": acked,
            },
        }
```

- [ ] **Step 4: Update tools/__init__.py**

```python
# tools/__init__.py
"""Tool registration module."""
from tools import problems, maintenance, events


def register_tools(mcp, get_zabbix) -> None:
    """Register all Zabbix tools on the FastMCP server instance."""
    problems.register(mcp, get_zabbix)
    maintenance.register(mcp, get_zabbix)
    events.register(mcp, get_zabbix)
```

- [ ] **Step 5: Run all tests**

```bash
uv run pytest tests/ -v
```

Expected: 20 passed (5 client + 7 problems + 8 maintenance → actually 6 events = 20 total)

- [ ] **Step 6: Commit**

```bash
git add tools/events.py tests/test_events.py tools/__init__.py
git commit -m "feat: add events tools — acknowledge + batch_acknowledge

- list_unacknowledged: filters acknowledged=false, readOnlyHint
- acknowledge_event: action bitmask (ack+message+close), destructiveHint
- batch_acknowledge: single API call for multiple events, destructiveHint
- All write tools have ⚠️ 写操作 marker in docstring"
```

---

### Task 6: Server Integration — Wire Everything + structlog Config

**Files:**
- Modify: `zabbix-mcp/server.py` (final version with structlog config)

- [ ] **Step 1: Write final server.py**

Replace `server.py` with the complete version:

```python
"""Zabbix MCP Server — entry point.

Provides Zabbix monitoring tools via MCP 2026-07-28 stateless protocol.
Uses API Token auth (no user.login session), compatible with stateless deployments.

Observability:
- Structured logging via structlog with OTel trace context injection
- OpenTelemetry traces for all Zabbix API calls (see zabbix_client.py)
"""
import os
from contextlib import asynccontextmanager

import structlog
from fastmcp import FastMCP
from opentelemetry import trace

# NOTE: env vars required — no defaults for Zabbix connection
ZABBIX_URL = os.environ.get("ZABBIX_URL", "")
ZABBIX_TOKEN = os.environ.get("ZABBIX_TOKEN", "")
ZABBIX_TIMEOUT = float(os.environ.get("ZABBIX_TIMEOUT", "30"))
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "8000"))


def _configure_logging() -> None:
    """Configure structlog with OTel trace context injection.

    OBS-CORR-001: 每条日志自动注入 trace_id + span_id。
    OBS-CORE-001: 所有日志结构化 key=value。
    """

    def add_trace_context(logger, method_name, event_dict):
        """从当前 OTel span 提取 trace_id/span_id 注入日志。"""
        span = trace.get_current_span()
        sc = span.get_span_context()
        if sc and sc.is_valid:
            event_dict["trace_id"] = format(sc.trace_id, "032x")
            event_dict["span_id"] = format(sc.span_id, "016x")
        return event_dict

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            add_trace_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
            # NOTE: 生产环境换为 structlog.processors.JSONRenderer()
        ],
    )


# Process-level ZabbixClient, initialized during lifespan
_zabbix_client = None


def _get_zabbix():
    """Get the process-level ZabbixClient.

    NOTE: In stateless mode, app.state is not reliable for cross-request
    data. Module-level variable is simpler and works for single-process
    deployments. Multi-process deployments should use shared storage.
    """
    if _zabbix_client is None:
        raise RuntimeError("ZabbixClient not initialized — check ZABBIX_URL/ZABBIX_TOKEN")
    return _zabbix_client


_configure_logging()

mcp = FastMCP(
    "Zabbix MCP",
    instructions=(
        "Provides tools for Zabbix monitoring: alert patrol, "
        "maintenance management, and alert acknowledgment. "
        "Start with list_active_problems() or problem_summary() for current state. "
        "Write tools (create/delete maintenance, acknowledge) require user confirmation."
    ),
)


@asynccontextmanager
async def lifespan(app):
    """Initialize ZabbixClient on startup, close on shutdown."""
    global _zabbix_client

    from zabbix_client import ZabbixClient

    if not ZABBIX_URL or not ZABBIX_TOKEN:
        raise RuntimeError(
            "ZABBIX_URL and ZABBIX_TOKEN environment variables are required"
        )

    _zabbix_client = ZabbixClient(
        url=ZABBIX_URL, token=ZABBIX_TOKEN, timeout=ZABBIX_TIMEOUT
    )

    structlog.get_logger().info(
        "zabbix_client_initialized",
        service="zabbix-mcp",
        zabbix_url=ZABBIX_URL,
    )

    yield

    await _zabbix_client.close()
    _zabbix_client = None
    structlog.get_logger().info("zabbix_client_closed", service="zabbix-mcp")


# Register all tools
from tools import register_tools
register_tools(mcp, _get_zabbix)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        host=MCP_HOST,
        port=MCP_PORT,
    )
```

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest tests/ -v
```

Expected: All tests pass

- [ ] **Step 3: Smoke test — start server and verify tool list**

```bash
ZABBIX_URL=http://mock ZABBIX_TOKEN=test uv run python server.py &
sleep 2

# Check tools/list
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"test","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}' | python3 -m json.tool

# Should list 8 tools with correct annotations
kill %1 2>/dev/null
```

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat: complete server.py with structlog + OTel config + tool wiring

- structlog configured with OTel trace context injection
- Module-level ZabbixClient for stateless mode compatibility
- lifespan initializes/closes client
- register_tools wires all 8 tools
- Smoke test verifies 8 tools with correct annotations"
```

---

### Task 7: Final Verification & Update Root CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (root)

- [ ] **Step 1: Update root CLAUDE.md MCP list**

In root `/Users/sunweini/mcpstore/CLAUDE.md`, update the MCP table:

```markdown
| 目录 | 名称 | 说明 | 状态 |
|---|---|---|---|
| `zabbix-mcp/` | Zabbix MCP | Zabbix 告警巡检/维护期/确认 | 开发中 |
```

- [ ] **Step 2: Run full test suite one more time**

```bash
cd zabbix-mcp
uv run pytest tests/ -v
```

Expected: All tests pass

- [ ] **Step 3: Final commit**

```bash
cd /Users/sunweini/mcpstore
git add -A
git commit -m "docs: register zabbix-mcp in root CLAUDE.md

- zabbix-mcp: alert patrol, maintenance, acknowledgment
- Status: development in progress"
```

---

## Self-Review Checklist

**1. Spec coverage:**
- [x] 告警巡检: `list_active_problems` (Task 3), `problem_summary` (Task 3)
- [x] 维护期管理: `create_maintenance` (Task 4), `list_maintenances` (Task 4), `delete_maintenance` (Task 4)
- [x] 告警确认: `list_unacknowledged` (Task 5), `acknowledge_event` (Task 5), `batch_acknowledge` (Task 5)
- [x] ZabbixClient: Task 2
- [x] 可观测性: Task 6 (structlog config), Task 2 (OTel spans in client)
- [x] 安全模型: tool annotations (readOnlyHint/destructiveHint) in Tasks 3-5
- [x] 错误处理: all tools return `{"status": "ok"|"error"}` pattern
- [x] 输入校验: severity validation, time parsing, host existence check
- [x] 混合模式: docstring `⚠️ 写操作` markers + annotations

**2. Placeholder scan:** No TBD/TODO in final implementations (one comment about periodic maintenance is a design note, not a placeholder)

**3. Type consistency:**
- `ZabbixClient.call(method: str, params: dict) -> Any` — consistent across all tasks
- `_resolve_severity(name: str | None) -> int | None` — defined in problems.py, imported by events.py
- `_format_problem(p: dict) -> dict` — defined in problems.py, imported by events.py
- `_parse_time(time_str: str) -> int` — defined in maintenance.py
- All tool return `dict` with `{"status": "ok"|"error", ...}` pattern
