# 全量调用明细审计实施计划（MySQL）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建 MySQL 实例记录所有 tools/call（成功+失败）元数据，dashboard 聚合统计与明细面板都从 MySQL 查--重启不丢、SQL 聚合原生。Redis 继续管配置/状态/失败审计。

**Architecture:** docker-compose 加 mysql:8 容器（库 mcp_audit / 表 calls）。gateway-proxy 的 on_call_tool 每次调用 INSERT 一行到 calls 表（旁路，失败不阻断）。gateway-admin 的 dashboard 聚合端点改 SQL 查询（替换 Prometheus），新增 /api/calls 明细分页。Redis 的 audit:failures 保留不动。

**Tech Stack:** MySQL 8.0 + aiomysql（async 驱动）、FastMCP middleware、FastAPI、Vue 3、pytest + fakeredis（Redis 部分）/ aiomysql 测试桩。

## Global Constraints

- MySQL 8.0 容器，库 `mcp_audit`，表 `calls`，容器内 3306 不映射宿主
- 字段元数据 only：time/server/tool/op/token_name/latency_ms/status/error_type/trace（不含参数/响应）
- 审计旁路：MySQL 写入失败仅记日志，不阻断主请求
- 只审计 tools/call；tools/list/ping 不记
- Redis 不动：servers/tokens/key 池/audit:failures 保留
- 失败双写：Redis audit:failures（现有）+ MySQL calls（status=fail）
- 依赖：gateway-proxy + gateway-admin 的 pyproject 加 `aiomysql>=0.2`
- 注释写"为什么"；结构化日志 structlog；key 明文禁入审计（token_name 是名称）

---

## 文件结构

| 文件 | 改动 |
|---|---|
| `deploy/docker-compose.yml` | 加 mysql 服务 + data 卷 |
| `deploy/config/mysql-init/01_calls.sql` | 新建建表 SQL |
| `deploy/config/mysql.env.example` | 新建（MYSQL_ROOT_PASSWORD 等） |
| `deploy/deploy.sh` | 加 mysql logs 目录、env 检查 |
| `gateway-proxy/db.py` | 新建 aiomysql 连接池单例 |
| `gateway-proxy/audit.py` | record_call 改 MySQL INSERT |
| `gateway-proxy/middleware.py` | 加 record_call_audit 辅助 |
| `gateway-proxy/permission_middleware.py` | on_call_tool 三路径接线 |
| `gateway-proxy/pyproject.toml` | 加 aiomysql |
| `gateway-proxy/tests/test_audit.py` | record_call 测试 |
| `gateway-proxy/tests/test_permission_middleware.py` | on_call_tool 写 MySQL 测试 |
| `gateway-admin/db.py` | 新建 aiomysql 连接池 |
| `gateway-admin/api/calls.py` | 新建 GET /api/calls |
| `gateway-admin/api/dashboard.py` | 聚合端点改 MySQL |
| `gateway-admin/app.py` | 注册 calls router |
| `gateway-admin/pyproject.toml` | 加 aiomysql |
| `gateway-admin/admin-ui/src/views/Calls.vue` | 新建请求日志页 |
| `gateway-admin/admin-ui/src/api/index.js` | 加 getCalls |
| `gateway-admin/admin-ui/src/router/index.js` | 加 /calls |
| `gateway-admin/admin-ui/src/components/Sidebar.vue` | 加菜单项 |
| `gateway-admin/tests/test_calls.py` | 新建 |

---

### Task 1: MySQL 容器 + 建表

**Files:**
- Modify: `deploy/docker-compose.yml`
- Create: `deploy/config/mysql-init/01_calls.sql`, `deploy/config/mysql.env.example`
- Modify: `deploy/deploy.sh`

**Interfaces:**
- Produces: mysql:8 容器（容器名 `mysql`，库 `mcp_audit`，用户 `mcp`），其他容器经 `mysql:3306` 连接

- [ ] **Step 1: 建表 SQL**（`deploy/config/mysql-init/01_calls.sql`）

```sql
-- 调用审计表：全量 tools/call（成功+失败），dashboard 聚合与明细面板的数据源
CREATE TABLE IF NOT EXISTS calls (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  time DATETIME(3) NOT NULL,
  server VARCHAR(64) NOT NULL,
  tool VARCHAR(128) NOT NULL,
  op VARCHAR(8) NOT NULL DEFAULT 'read',
  token_name VARCHAR(128) NOT NULL DEFAULT '',
  latency_ms INT NOT NULL DEFAULT 0,
  status VARCHAR(8) NOT NULL,
  error_type VARCHAR(32) NOT NULL DEFAULT '',
  trace VARCHAR(64) NOT NULL DEFAULT '',
  INDEX idx_time (time),
  INDEX idx_server (server),
  INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

- [ ] **Step 2: env 模板**（`deploy/config/mysql.env.example`）

```bash
# MySQL 审计库（调用明细 + 聚合统计）
MYSQL_ROOT_PASSWORD=change_me_strong
MYSQL_USER=mcp
MYSQL_PASSWORD=change_me_strong
MYSQL_DATABASE=mcp_audit
```

- [ ] **Step 3: compose 加 mysql 服务**（`deploy/docker-compose.yml`，redis 服务后）

```yaml
  mysql:
    image: mysql:8.0
    env_file:
      - ./config/mysql.env
    volumes:
      - ./data/mysql:/var/lib/mysql
      - ./config/mysql-init:/docker-entrypoint-initdb.d:ro
    networks: [mcp-net]
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost", "-p$MYSQL_ROOT_PASSWORD"]
      interval: 10s
      timeout: 5s
      retries: 10
    restart: unless-stopped
```

给 gateway-proxy 和 gateway-admin 服务加 env_file `./config/mysql.env` + environment `MYSQL_URL: mysql://mcp:$$MYSQL_PASSWORD@mysql:3306/mcp_audit`（注意 compose 里 `$$` 转义）。

- [ ] **Step 4: deploy.sh 加 mysql 日志目录**（`deploy/deploy.sh` 的 mkdir 行加 `"$LOGS_DIR/mysql"`，env 检查段加 mysql.env）

- [ ] **Step 5: 本地验证 compose 语法 + 起库**

```bash
cd deploy && cp config/mysql.env.example config/mysql.env
# 编辑真实密码
docker compose config >/dev/null && echo "compose OK"
docker compose up -d mysql
sleep 20
docker exec deploy-mysql-1 mysql -umcp -p$MYSQL_PASSWORD -e "USE mcp_audit; SHOW TABLES;"  # 应见 calls
docker compose down
```
Expected: `compose OK` + tables 列出 `calls`

- [ ] **Step 6: Commit**

```bash
git add deploy/
git commit -m "feat(deploy): add MySQL 8 container for call audit (mcp_audit.calls table)"
```

---

### Task 2: gateway-proxy MySQL 连接池 + record_call

**Files:**
- Create: `gateway-proxy/db.py`
- Modify: `gateway-proxy/audit.py`（record_call 改 MySQL）
- Modify: `gateway-proxy/pyproject.toml`（加 aiomysql）
- Test: `gateway-proxy/tests/test_audit.py`

**Interfaces:**
- Consumes: `MYSQL_URL` env
- Produces: `db.get_pool() -> aiomysql.Pool`、`audit.record_call(meta, status, error_type=None) -> None`

- [ ] **Step 1: pyproject 加依赖**

`gateway-proxy/pyproject.toml` dependencies 加 `"aiomysql>=0.2"`，`uv sync`。

- [ ] **Step 2: db.py 连接池单例**

```python
"""aiomysql 连接池单例。MySQL 专管调用审计（calls 表），Redis 管配置/状态。

连接串从 MYSQL_URL 解析（mysql://user:pass@host:port/db）。proxy 启动后首次
record_call 时懒加载池；池断线 aiomysql 自动重连。
"""
import os
from urllib.parse import urlparse

import aiomysql

_pool: aiomysql.Pool | None = None


def _parse_url(url: str) -> dict:
    p = urlparse(url)
    return {
        "host": p.hostname or "mysql",
        "port": p.port or 3306,
        "user": p.username or "mcp",
        "password": p.password or "",
        "db": (p.path or "/mcp_audit").lstrip("/"),
    }


async def get_pool() -> aiomysql.Pool:
    """懒加载连接池。首次调用创建，之后复用。"""
    global _pool
    if _pool is None:
        url = os.environ.get("MYSQL_URL", "")
        if not url:
            raise RuntimeError("MYSQL_URL not configured")
        cfg = _parse_url(url)
        _pool = await aiomysql.create_pool(
            minsize=2, maxsize=10, autocommit=True, **cfg,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
```

- [ ] **Step 3: 写失败测试**（追加到 `tests/test_audit.py`）

```python
# ─── record_call (MySQL) ─────────────────────────────────────────

async def test_record_call_inserts_row(monkeypatch):
    """record_call 向 calls 表插一行，字段正确。"""
    import audit
    inserted = []

    class FakeCursor:
        async def execute(self, sql, args):
            inserted.append((sql, args))
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class FakeConn:
        async def cursor(self): return FakeCursor()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class FakePool:
        def acquire(self):
            class Cm:
                async def __aenter__(self): return FakeConn()
                async def __aexit__(self, *a): pass
            return Cm()

    monkeypatch.setattr(audit, "get_pool", lambda: _coro(FakePool()))
    await audit.record_call(
        meta={"trace_id": "t1", "server": "tavily-mcp", "tool": "tavily_search",
              "op": "read", "token_name": "tok", "latency_ms": 42,
              "time": "2026-08-04 10:00:00.000"},
        status="ok",
    )
    assert len(inserted) == 1
    sql, args = inserted[0]
    assert "INSERT INTO calls" in sql
    assert args[1] == "tavily-mcp"  # server
    assert args[6] == "ok"          # status


async def test_record_call_db_failure_does_not_raise(monkeypatch):
    """MySQL 异常不抛出（旁路审计不阻断主请求）。"""
    import audit
    async def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(audit, "get_pool", boom)
    await audit.record_call(
        meta={"trace_id": "t", "server": "s", "tool": "t", "op": "read",
              "token_name": "n", "latency_ms": 1, "time": "2026-08-04 10:00:00.000"},
        status="ok",
    )  # 不抛


# 辅助：把对象包成 awaitable
async def _coro(obj):
    return obj
```

- [ ] **Step 4: 运行确认失败**

Run: `cd gateway-proxy && uv run python -m pytest tests/test_audit.py -v -k record_call`
Expected: FAIL（record_call 还是 Redis 版或 get_pool 未 mock）

- [ ] **Step 5: 改写 audit.py record_call**

audit.py 顶部加 `from db import get_pool`，删除现有 `record_call`（Redis 版），替换为：

```python
async def record_call(meta: dict, status: str, error_type: str | None = None) -> None:
    """INSERT 调用记录到 MySQL calls 表。旁路：失败仅记日志不阻断。"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO calls (time, server, tool, op, token_name, "
                    "latency_ms, status, error_type, trace) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (meta["time"], meta["server"], meta["tool"], meta["op"],
                     meta["token_name"], meta["latency_ms"], status,
                     error_type or "", meta["trace_id"]),
                )
    except Exception as e:
        logger.error("audit_call_write_failed", error=str(e), service="gateway-proxy")
```

（保留现有 `record_failure` Redis 版不动）

- [ ] **Step 6: 跑测试确认通过**

Run: `cd gateway-proxy && uv run python -m pytest tests/test_audit.py -v -k record_call`
Expected: PASS（2 个）

- [ ] **Step 7: Commit**

```bash
git add gateway-proxy/db.py gateway-proxy/audit.py gateway-proxy/pyproject.toml gateway-proxy/tests/test_audit.py
git commit -m "feat(gateway-proxy): MySQL pool + record_call INSERT to calls table"
```

---

### Task 3: middleware record_call_audit + on_call_tool 接线

**Files:**
- Modify: `gateway-proxy/middleware.py`（加 record_call_audit）
- Modify: `gateway-proxy/permission_middleware.py`（on_call_tool 三路径）
- Test: `gateway-proxy/tests/test_permission_middleware.py`

**Interfaces:**
- Consumes: Task 2 `audit.record_call`、`routing.resolve_target`、现有 `record_call_failure`
- Produces: `record_call_audit(token_info, mcp_name, latency_ms, trace_id, status, error_type=None)`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_permission_middleware.py`）

```python
# ─── on_call_tool 写 MySQL calls ─────────────────────────────────

async def test_call_success_writes_calls(fake_redis, monkeypatch):
    """成功调用 -> record_call 被调一次 status=ok。"""
    from auth import hash_token
    await fake_redis.hset(
        f"tokens:{hash_token('tok_ok')}",
        mapping={"id": "t1", "name": "caller",
                 "permissions": '{"zabbix": {"read": true, "write": false}}'},
    )
    monkeypatch.setattr("permission_middleware.get_http_headers",
                        lambda include=None: {"authorization": "Bearer tok_ok"})
    calls = []
    async def fake_record_call(meta, status, error_type=None):
        calls.append((meta, status, error_type))
    monkeypatch.setattr("middleware.record_call", fake_record_call)

    async def call_next(ctx): return "result"
    await PermissionMiddleware().on_call_tool(FakeContext("zabbix_list_active_problems"), call_next)

    assert len(calls) == 1
    meta, status, _ = calls[0]
    assert status == "ok"
    assert meta["server"] == "zabbix"
    assert meta["tool"] == "list_active_problems"
    assert meta["token_name"] == "caller"


async def test_call_denied_writes_calls_fail(fake_redis, monkeypatch):
    """权限拒绝 -> record_call status=fail + error_type，且 record_failure 仍调（Redis）。"""
    monkeypatch.setattr("permission_middleware.get_http_headers",
                        lambda include=None: {})  # 无 token
    calls = []
    async def fake_record_call(meta, status, error_type=None):
        calls.append((status, error_type))
    monkeypatch.setattr("middleware.record_call", fake_record_call)

    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        await PermissionMiddleware().on_call_tool(
            FakeContext("zabbix_list_active_problems"), lambda ctx: "x")
    assert calls == [("fail", "invalid_token")]


async def test_call_exception_writes_calls_fail(fake_redis, monkeypatch):
    """后端异常 -> record_call status=fail + upstream_error。"""
    from auth import hash_token
    await fake_redis.hset(f"tokens:{hash_token('tok_rw')}",
        mapping={"id": "t2", "name": "c2", "permissions": '{"zabbix": {"read": true, "write": true}}}'})
    monkeypatch.setattr("permission_middleware.get_http_headers",
                        lambda include=None: {"authorization": "Bearer tok_rw"})
    calls = []
    async def fake_record_call(meta, status, error_type=None):
        calls.append((status, error_type))
    monkeypatch.setattr("middleware.record_call", fake_record_call)

    async def call_next(ctx): raise RuntimeError("backend down")
    with pytest.raises(RuntimeError):
        await PermissionMiddleware().on_call_tool(
            FakeContext("zabbix_create_maintenance"), call_next)
    assert calls == [("fail", "upstream_error")]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway-proxy && uv run python -m pytest tests/test_permission_middleware.py -v -k writes_calls`
Expected: FAIL（calls 为空）

- [ ] **Step 3: middleware.py 加 record_call_audit**

顶部 import 改：`from audit import record_failure, record_call`
紧接 `record_call_failure` 后加：

```python
async def record_call_audit(
    token_info: dict | None,
    mcp_name: str,
    latency_ms: int,
    trace_id: str,
    status: str,
    error_type: str | None = None,
) -> None:
    """写全量调用明细到 MySQL calls 表（成功+失败均写）。

    与 record_call_failure 互补：failures 流（Redis）供失败面板，
    calls 表（MySQL）供请求日志页 + 聚合统计。失败条目双写。
    """
    server, tool, op = "", "", "read"
    try:
        server, tool, op = resolve_target(mcp_name)
    except (ValueError, UnknownServerError):
        pass
    token_name = token_info.get("name", "(anonymous)") if token_info else "(anonymous)"
    await record_call(
        meta={
            "trace_id": trace_id,
            "server": server,
            "tool": tool,
            "op": op,
            "token_name": token_name,
            "latency_ms": latency_ms,
            "time": time.strftime("%Y-%m-%d %H:%M:%S.000", time.gmtime()),
        },
        status=status,
        error_type=error_type,
    )
```

- [ ] **Step 4: on_call_tool 三路径接线**（`permission_middleware.py`）

import 改：`from middleware import check_call_permission, classify_error, record_call_failure, record_call_audit`

- **拒绝路径**（`if not allowed:` 块，`raise ToolError` 前，record_call_failure 后）加：
```python
            await record_call_audit(token_info, tool_name, latency_ms, trace_id, "fail", error_type)
```
- **异常路径**（`except Exception` 块，`raise` 前，record_call_failure 后）加：
```python
            await record_call_audit(token_info, tool_name, latency_ms, trace_id, "fail", err_type)
```
- **成功路径**（`return result` 前）加：
```python
        await record_call_audit(token_info, tool_name, latency_ms, trace_id, "ok")
```

- [ ] **Step 5: 跑测试 + 全量回归**

Run: `cd gateway-proxy && uv run python -m pytest tests/ -q`
Expected: 全过

- [ ] **Step 6: Commit**

```bash
git add gateway-proxy/middleware.py gateway-proxy/permission_middleware.py gateway-proxy/tests/test_permission_middleware.py
git commit -m "feat(gateway-proxy): on_call_tool records all calls to MySQL (success+fail)"
```

---

### Task 4: gateway-admin db.py + /api/calls

**Files:**
- Create: `gateway-admin/db.py`（镜像 proxy 的 db.py）
- Create: `gateway-admin/api/calls.py`
- Modify: `gateway-admin/app.py`、`gateway-admin/pyproject.toml`
- Test: `gateway-admin/tests/test_calls.py`

**Interfaces:**
- Produces: `GET /api/calls?server=&status=&limit=&offset=` -> `{count, data:[...]}`

- [ ] **Step 1: pyproject 加 aiomysql，db.py 复制 proxy 版**

`gateway-admin/pyproject.toml` 加 `"aiomysql>=0.2"`。
`gateway-admin/db.py` = Task 2 的 db.py（复制，同 get_pool/close_pool）。

- [ ] **Step 2: 写失败测试**（`tests/test_calls.py`）

```python
"""Tests for /api/calls - 请求明细（MySQL calls 表）。"""
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def test_list_calls_requires_auth(client):
    assert client.get("/api/calls").status_code == 401


def test_list_calls_empty(client, auth_headers, monkeypatch):
    """空表 -> count 0。"""
    async def fake_query(sql, args=None):
        return []
    monkeypatch.setattr("api.calls.query_calls", lambda **kw: _coro([]))
    resp = client.get("/api/calls", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "data": []}


async def _coro(v): return v


def test_list_calls_returns_rows(client, auth_headers, monkeypatch):
    rows = [
        {"id": 2, "time": "2026-08-04 10:00:01", "server": "serpapi-mcp",
         "tool": "serpapi_baidu", "op": "read", "token_name": "b",
         "latency_ms": 5, "status": "fail", "error_type": "upstream_error", "trace": "t2"},
        {"id": 1, "time": "2026-08-04 10:00:00", "server": "tavily-mcp",
         "tool": "tavily_search", "op": "read", "token_name": "a",
         "latency_ms": 42, "status": "ok", "error_type": "", "trace": "t1"},
    ]
    monkeypatch.setattr("api.calls.query_calls", lambda **kw: _coro(rows))
    resp = client.get("/api/calls", headers=auth_headers)
    body = resp.json()
    assert body["count"] == 2
    assert body["data"][0]["trace"] == "t2"
```

- [ ] **Step 3: 运行确认失败**

Run: `cd gateway-admin && uv run python -m pytest tests/test_calls.py -v`
Expected: FAIL（404 路由不存在）

- [ ] **Step 4: 实现 api/calls.py**

```python
"""请求明细 API：读 MySQL calls 表（全量 tools/call，成功+失败）。

与 /api/failures（Redis 失败流）互补：calls 含全部，failures 只含失败。
"""
from fastapi import APIRouter, Depends, Query

from auth import require_admin
from db import get_pool

router = APIRouter(prefix="/api/calls", tags=["calls"])


async def query_calls(server: str | None, status: str | None,
                      limit: int, offset: int) -> list[dict]:
    """SQL 分页查询 calls 表，倒序（最新 id 在前）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            where = []
            args = []
            if server:
                where.append("server = %s"); args.append(server)
            if status:
                where.append("status = %s"); args.append(status)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            await cur.execute(
                f"SELECT id, time, server, tool, op, token_name, latency_ms, "
                f"status, error_type, trace FROM calls {clause} "
                f"ORDER BY id DESC LIMIT %s OFFSET %s",
                args + [limit, offset],
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in await cur.fetchall()]


@router.get("")
async def list_calls(
    server: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: str = Depends(require_admin),
):
    rows = await query_calls(server, status, limit, offset)
    return {"count": len(rows), "data": rows}
```

- [ ] **Step 5: app.py 注册 router**

```python
from api import servers, tokens, dashboard, keys, calls
app.include_router(calls.router)
```

- [ ] **Step 6: 跑测试 + 全量回归**

Run: `cd gateway-admin && uv run python -m pytest tests/ -q`
Expected: 全过

- [ ] **Step 7: Commit**

```bash
git add gateway-admin/db.py gateway-admin/api/calls.py gateway-admin/app.py gateway-admin/pyproject.toml gateway-admin/tests/test_calls.py
git commit -m "feat(gateway-admin): db.py + /api/calls endpoint for call audit"
```

---

### Task 5: dashboard 聚合改 MySQL

**Files:**
- Modify: `gateway-admin/api/dashboard.py`（summary/by-server/timeseries 改 SQL）
- Test: `gateway-admin/tests/test_dashboard.py`

**Interfaces:**
- Produces: `/api/metrics/summary`、`/api/metrics/by-server`、`/api/metrics/timeseries` 改从 MySQL 聚合（响应结构保持兼容，前端不改）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_dashboard.py`）

```python
def test_metrics_summary_from_mysql(client, auth_headers, monkeypatch):
    """summary 从 MySQL 聚合，不再查 Prometheus。"""
    async def fake_pool():
        class P:
            def acquire(self):
                class C:
                    async def __aenter__(self):
                        class Conn:
                            async def cursor(self):
                                class Cur:
                                    async def execute(self, s, a=None): pass
                                    async def fetchone(self): return (100, 5, 200, 50)  # total,fail,avg,max
                                    async def fetchall(self): return []
                                    description = []
                                return Cur()
                            async def __aenter__(self): return self
                            async def __aexit__(self, *a): pass
                        return Conn()
                    async def __aexit__(self, *a): pass
                return C()
        return P()
    monkeypatch.setattr("db.get_pool", fake_pool)
    resp = client.get("/api/metrics/summary", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["requests"] == 100
    assert body["errors"] == 5
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway-admin && uv run python -m pytest tests/test_dashboard.py -v -k from_mysql`
Expected: FAIL（仍查 Prometheus，值不匹配）

- [ ] **Step 3: 改写 dashboard.py 三个端点**

把 `metrics_summary`、`metrics_by_server`、`metrics_timeseries` 的 Prometheus 查询替换为 MySQL 聚合 SQL：

```python
from db import get_pool

WINDOW_24H = "DATE_SUB(NOW(), INTERVAL 24 HOUR)"
WINDOW_1H = "DATE_SUB(NOW(), INTERVAL 1 HOUR)"


@router.get("/metrics/summary")
async def metrics_summary(server: str | None = None, _: str = Depends(require_admin)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            where = f"WHERE time > {WINDOW_24H}" + (f" AND server=%s" if server else "")
            args = [server] if server else []
            await cur.execute(
                f"SELECT COUNT(*), SUM(status='fail'), AVG(latency_ms), MAX(latency_ms) "
                f"FROM calls {where}", args)
            total, errors, avg_lat, max_lat = await cur.fetchone()
            # P95：按延迟排序取第 95 百分位
            await cur.execute(
                f"SELECT latency_ms FROM calls {where} ORDER BY latency_ms", args)
            lats = [r[0] for r in await cur.fetchall()]
            p95 = lats[int(len(lats) * 0.95)] if lats else 0
    return {
        "requests": int(total or 0),
        "errors": int(errors or 0),
        "error_rate": round((errors or 0) / total, 4) if total else 0.0,
        "p95_ms": int(p95),
        "avg_ms": int(avg_lat or 0),
    }


@router.get("/metrics/by-server")
async def metrics_by_server(_: str = Depends(require_admin)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT server, COUNT(*), SUM(status='fail') FROM calls "
                f"WHERE time > {WINDOW_24H} GROUP BY server")
            rows = await cur.fetchall()
    return [{"server": r[0], "requests": int(r[1]), "errors": int(r[2] or 0)} for r in rows]


@router.get("/metrics/timeseries")
async def metrics_timeseries(server: str | None = None, window: str = "1h",
                             _: str = Depends(require_admin)):
    interval = "1 HOUR" if window == "1h" else "24 HOUR"
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            where = f"WHERE time > DATE_SUB(NOW(), INTERVAL {interval})"
            if server: where += " AND server=%s"
            await cur.execute(
                f"SELECT FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(time)/60)*60) AS bucket, "
                f"COUNT(*), SUM(status='fail') FROM calls {where} "
                f"GROUP BY bucket ORDER BY bucket",
                [server] if server else [])
            rows = await cur.fetchall()
    return [{"time": str(r[0]), "requests": int(r[1]), "errors": int(r[2] or 0)} for r in rows]
```

`/api/failures`（读 Redis audit:failures）**保留不动**。

- [ ] **Step 4: 跑测试 + 全量回归**

Run: `cd gateway-admin && uv run python -m pytest tests/ -q`
Expected: 全过（现有 Prometheus mock 测试若失败需同步改--它们断言旧数据源，改为 MySQL mock）

- [ ] **Step 5: Commit**

```bash
git add gateway-admin/api/dashboard.py gateway-admin/tests/test_dashboard.py
git commit -m "feat(gateway-admin): dashboard aggregates from MySQL (survives restart)"
```

---

### Task 6: 前端「请求日志」页

**Files:**
- Create: `gateway-admin/admin-ui/src/views/Calls.vue`
- Modify: `api/index.js`、`router/index.js`、`components/Sidebar.vue`

- [ ] **Step 1: api/index.js 加 getCalls**

```javascript
export function getCalls(params = {}) {
  const p = new URLSearchParams()
  if (params.server) p.set('server', params.server)
  if (params.status) p.set('status', params.status)
  if (params.limit) p.set('limit', params.limit)
  if (params.offset) p.set('offset', params.offset)
  return apiFetch(`/api/calls?${p}`)
}
```

- [ ] **Step 2: Calls.vue**（参考 APIKeys.vue）

```vue
<template>
  <div>
    <h2>请求日志</h2>
    <div class="filters">
      <select v-model="filterServer" @change="reload">
        <option value="">全部 Server</option>
        <option v-for="s in servers" :key="s" :value="s">{{ s }}</option>
      </select>
      <select v-model="filterStatus" @change="reload">
        <option value="">全部状态</option>
        <option value="ok">成功</option>
        <option value="fail">失败</option>
      </select>
      <button class="btn" @click="reload">刷新</button>
    </div>
    <table>
      <thead><tr><th>时间</th><th>Server</th><th>Tool</th><th>Token</th><th>操作</th><th>耗时</th><th>状态</th></tr></thead>
      <tbody>
        <tr v-for="c in calls" :key="c.id" :class="{ 'row-fail': c.status === 'fail' }">
          <td>{{ c.time }}</td><td>{{ c.server }}</td><td>{{ c.tool }}</td>
          <td>{{ c.token_name }}</td><td>{{ c.op === 'write' ? '写' : '读' }}</td>
          <td>{{ c.latency_ms }}ms</td>
          <td><span v-if="c.status === 'ok'" class="ok">✓</span>
              <span v-else class="fail">✗ {{ c.error_type }}</span></td>
        </tr>
      </tbody>
    </table>
    <div v-if="!calls.length" class="empty">暂无调用记录</div>
    <div class="pager">
      <button :disabled="offset === 0" @click="prev">上一页</button>
      <button :disabled="calls.length < limit" @click="next">下一页</button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getCalls } from '../api'
const servers = ['tavily-mcp', 'brave-mcp', 'serpapi-mcp', 'zabbix-mcp']
const calls = ref([])
const filterServer = ref('')
const filterStatus = ref('')
const limit = 50
const offset = ref(0)
async function reload() { offset.value = 0; await load() }
async function load() { calls.value = (await getCalls({ server: filterServer.value, status: filterStatus.value, limit, offset: offset.value })).data }
function prev() { offset.value = Math.max(0, offset.value - limit); load() }
function next() { offset.value += limit; load() }
onMounted(reload)
</script>

<style scoped>
.filters { margin-bottom: 16px; display: flex; gap: 12px; align-items: center; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
.row-fail { background: rgba(255,90,90,0.06); }
.ok { color: #3fb950; } .fail { color: #f85149; }
.empty { padding: 32px; text-align: center; color: var(--text-dim); }
.pager { margin-top: 16px; display: flex; gap: 8px; }
</style>
```

- [ ] **Step 3: router 加 `/calls`，Sidebar 加「请求日志」菜单项**（API Keys 之后）

router：`{ path: '/calls', name: 'calls', component: () => import('../views/Calls.vue') }`
Sidebar navItems 加：`{ id: 'calls', label: '请求日志', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 12h4M3 6h4M3 18h4M10 12h11M10 6h11M10 18h11"/></svg>' }`

- [ ] **Step 4: 构建前端**

```bash
cd gateway-admin/admin-ui && npm run build
```
Expected: dist 生成无错误

- [ ] **Step 5: Commit**

```bash
git add gateway-admin/admin-ui/
git commit -m "feat(gateway-admin): 请求日志 page (Calls.vue) with filters + pagination"
```

---

### Task 7: 部署 + 端到端验证

**Files:**
- 无新文件（部署 + 验证）

- [ ] **Step 1: 同步代码到生产**

```bash
git archive --format=tar.gz -o /tmp/deploy.tar.gz HEAD
scp -i ~/.ssh/id_loginmonitor -P 9166 /tmp/deploy.tar.gz root@10.33.17.72:/tmp/
ssh root@10.33.17.72 "cd /opt/mcp-gateway-cfg && tar xzf /tmp/deploy.tar.gz"
```

- [ ] **Step 2: 配 mysql.env 真实密码 + 重建全部容器**

```bash
ssh root@10.33.17.72 "cd /opt/mcp-gateway-cfg/deploy && cp config/mysql.env.example config/mysql.env && vi config/mysql.env  # 填真实密码
bash deploy.sh"
```
Expected: 8 容器全 Healthy（含 mysql）

- [ ] **Step 3: 端到端验证**

```bash
# 1. 经 proxy 调一个工具（产生 calls 记录）
# 2. GET /api/calls 应有记录
# 3. GET /api/metrics/summary 应有聚合
# 4. 浏览器 http://10.33.17.72:8081/calls 见请求日志页
# 5. 重启 proxy 容器 -> 聚合数据不丢（MySQL 持久）
```

- [ ] **Step 4: Commit 验证记录**

```bash
git commit --allow-empty -m "chore: verify call audit MySQL end-to-end on production"
```

---

## Self-Review 记录

**Spec 覆盖：**
- ✅ MySQL 新实例 + calls 表 -> Task 1
- ✅ proxy record_call MySQL -> Task 2
- ✅ on_call_tool 三路径写 -> Task 3
- ✅ admin /api/calls 明细 -> Task 4
- ✅ dashboard 聚合改 MySQL（重启不丢）-> Task 5
- ✅ 前端请求日志页 -> Task 6
- ✅ 部署验证 -> Task 7
- ✅ Redis 不动（failures 保留）-> 各任务注明
- ✅ 旁路不阻断 -> Task 2 测试
- ✅ 30 天保留 -> spec 提及（实现：admin 启动 DELETE，Task 5 可选加）

**类型一致性：**
- record_call(meta, status, error_type) Task 2 定义，Task 3 record_call_audit 调用一致
- record_call_audit(token_info, mcp_name, latency_ms, trace_id, status, error_type) Task 3 定义，on_call_tool 三路径一致
- query_calls(server, status, limit, offset) Task 4 定义，list_calls 调用一致
- /api/calls 返回 {count, data} Task 4，前端 Task 6 消费 .data 一致

**坑位预判：**
1. aiomysql create_pool 是 async，get_pool 要 `await`（Task 2 已 await）
2. compose `$$MYSQL_PASSWORD` 双美元转义（env_file 引用）
3. mysql-init 目录首启才执行建表 SQL；已有 data/mysql 卷时跳过（删卷重建才重跑）
4. dashboard 现有 Prometheus mock 测试需同步改 MySQL mock（Task 5 Step 4 注明）
5. P95 近似（排序取 offset）对大表慢；24h 窗口量级可接受，量大改分桶
6. time 字段 DATETIME(3)，meta["time"] 格式 `"%Y-%m-%d %H:%M:%S.000"`（Task 3 已对齐）
