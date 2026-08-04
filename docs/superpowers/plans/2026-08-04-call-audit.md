# 全量调用明细审计实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** gateway-proxy 记录所有 tools/call（成功+失败）元数据到 Redis Stream `audit:calls`，gateway-admin 加「请求日志」页展示逐条调用明细。

**Architecture:** PermissionMiddleware.on_call_tool 已计时并持有 token/server/tool 全部信息，在成功/失败两路径补写 `audit:calls`（失败仍另写 `audit:failures`，不破坏现有失败面板）。admin 加 `/api/calls` 读流 + Vue「请求日志」页。

**Tech Stack:** FastMCP 4.0.0b1 middleware、redis.asyncio Stream（xadd/xrevrange）、FastAPI、Vue 3、fakeredis（测试）。

## Global Constraints

- 新流 `audit:calls`，MAXLEN 50000；`audit:failures` 保留不动
- 字段元数据 only：trace/server/tool/op/token_name/latency_ms/status/error_type/time（**不含**请求参数/响应内容）
- 审计是旁路：写入失败仅记日志，不阻断主请求流程
- 只审计 tools/call（tools/list/ping 不记）
- 聚合计数仍用 Prometheus（不持久化，重启清零可接受）
- 注释写"为什么"；结构化日志 structlog
- key_id/明文 key 禁入审计（token_name 是名称非明文）

---

## 文件结构

| 文件 | 改动 |
|---|---|
| `gateway-proxy/audit.py` | 加 `record_call(meta, status, error_type=None)` -> xadd audit:calls |
| `gateway-proxy/middleware.py` | 加 `record_call_audit(...)` 辅助（构造 meta + 调 record_call） |
| `gateway-proxy/permission_middleware.py` | on_call_tool 三路径（成功/拒绝/异常）补 record_call_audit |
| `gateway-proxy/tests/test_audit.py` | 加 record_call 测试 |
| `gateway-proxy/tests/test_permission_middleware.py` | 加 on_call_tool 写 audit:calls 测试 |
| `gateway-admin/api/calls.py` | 新建 `GET /api/calls` |
| `gateway-admin/app.py` | 注册 calls router |
| `gateway-admin/tests/test_calls.py` | 新建 |
| `gateway-admin/admin-ui/src/views/Calls.vue` | 新建「请求日志」页 |
| `gateway-admin/admin-ui/src/api/index.js` | 加 getCalls |
| `gateway-admin/admin-ui/src/router/index.js` | 加 /calls 路由 |
| `gateway-admin/admin-ui/src/components/Sidebar.vue` | 加「请求日志」菜单项 |

**设计说明（偏离 spec 的 YAGNI 简化）**：spec 写 `record_call(journey, meta, status, error_type)`，但「请求日志」页不展示 journey（列：时间/Server/Tool/Token/操作/耗时/状态），且 failures 流已存 journey 供失败追踪。calls 流去掉 journey 字段，简化实现。

---

### Task 1: audit.py record_call + 测试

**Files:**
- Modify: `gateway-proxy/audit.py`
- Test: `gateway-proxy/tests/test_audit.py`

**Interfaces:**
- Consumes: `redis_client.get_redis()`（同 record_failure）
- Produces: `record_call(meta: dict, status: str, error_type: str | None = None) -> None`，写 `audit:calls` 流。meta 字段：`{trace_id, server, tool, op, token_name, latency_ms, time}`

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_audit.py`）

```python
# ─── record_call (audit:calls 全量调用明细) ───────────────────────

async def test_record_call_success_writes_audit_calls(fake_redis):
    """成功调用写 audit:calls，status=ok，无 error_type。"""
    from audit import record_call
    await record_call(
        meta={
            "trace_id": "abc123",
            "server": "tavily-mcp",
            "tool": "tavily_search",
            "op": "read",
            "token_name": "my-token",
            "latency_ms": 42,
            "time": "2026-08-04T10:00:00Z",
        },
        status="ok",
    )
    entries = await fake_redis.xrevrange("audit:calls", count=10)
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["status"] == "ok"
    assert fields["server"] == "tavily-mcp"
    assert fields["tool"] == "tavily_search"
    assert fields["latency_ms"] == "42"
    assert fields["error_type"] == ""


async def test_record_call_failure_includes_error_type(fake_redis):
    """失败调用写 audit:calls，status=fail，带 error_type。"""
    from audit import record_call
    await record_call(
        meta={
            "trace_id": "xyz",
            "server": "serpapi-mcp",
            "tool": "serpapi_baidu",
            "op": "read",
            "token_name": "(anonymous)",
            "latency_ms": 5,
            "time": "2026-08-04T10:00:01Z",
        },
        status="fail",
        error_type="permission_denied",
    )
    entries = await fake_redis.xrevrange("audit:calls", count=10)
    _, fields = entries[0]
    assert fields["status"] == "fail"
    assert fields["error_type"] == "permission_denied"


async def test_record_call_maxlen_trims(fake_redis):
    """audit:calls MAXLEN 50000：超过截断，旧条目丢失。"""
    from audit import _CALLS_MAXLEN
    # 用小 MAXLEN 验证截断逻辑（不真写 50000 条）
    import audit
    original = audit._CALLS_MAXLEN
    audit._CALLS_MAXLEN = 3
    try:
        for i in range(5):
            await audit.record_call(
                meta={"trace_id": str(i), "server": "s", "tool": "t",
                      "op": "read", "token_name": "n", "latency_ms": i,
                      "time": "2026-08-04T10:00:00Z"},
                status="ok",
            )
        entries = await fake_redis.xrevrange("audit:calls", count=100)
        assert len(entries) == 3  # 截断到 MAXLEN
    finally:
        audit._CALLS_MAXLEN = original


async def test_record_call_redis_failure_does_not_raise(fake_redis, monkeypatch):
    """Redis 写入异常不抛出（审计是旁路，不阻断主流程）。"""
    from audit import record_call
    async def boom(*a, **kw):
        raise RuntimeError("redis down")
    monkeypatch.setattr(fake_redis, "xadd", boom)
    # 不应抛异常
    await record_call(
        meta={"trace_id": "t", "server": "s", "tool": "t", "op": "read",
              "token_name": "n", "latency_ms": 1, "time": "2026-08-04T10:00:00Z"},
        status="ok",
    )
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway-proxy && uv run python -m pytest tests/test_audit.py -v -k record_call`
Expected: FAIL - `ImportError: cannot import name 'record_call'`

- [ ] **Step 3: 实现 record_call**（追加到 `gateway-proxy/audit.py`）

```python
# 全量调用明细流：所有 tools/call（成功+失败），与 audit:failures 独立。
# MAXLEN 50000 约数月数据；失败条目同时写 failures（现有失败面板依赖）。
_CALLS_STREAM = "audit:calls"
_CALLS_MAXLEN = 50000


async def record_call(
    meta: dict,
    status: str,
    error_type: str | None = None,
) -> None:
    """Append a call record (success or failure) to audit:calls.

    旁路审计：写入失败仅记日志，不影响主请求（spec 错误处理节）。
    meta: {trace_id, server, tool, op, token_name, latency_ms, time}
    status: "ok" | "fail"
    """
    r = get_redis()
    try:
        await r.xadd(_CALLS_STREAM, {
            "trace": meta["trace_id"],
            "server": meta["server"],
            "tool": meta["tool"],
            "op": meta["op"],
            "token_name": meta.get("token_name", ""),
            "latency_ms": meta["latency_ms"],
            "status": status,
            "error_type": error_type or "",
            "time": meta["time"],
        }, maxlen=_CALLS_MAXLEN, approximate=True)
    except Exception as e:
        logger.error("audit_call_write_failed", error=str(e), service="gateway-proxy")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd gateway-proxy && uv run python -m pytest tests/test_audit.py -v -k record_call`
Expected: PASS（4 个）

- [ ] **Step 5: Commit**

```bash
git add gateway-proxy/audit.py gateway-proxy/tests/test_audit.py
git commit -m "feat(gateway-proxy): record_call writes all tools/call to audit:calls stream"
```

---

### Task 2: middleware record_call_audit + on_call_tool 接线

**Files:**
- Modify: `gateway-proxy/middleware.py`（加 record_call_audit 辅助）
- Modify: `gateway-proxy/permission_middleware.py:84-130`（on_call_tool 三路径补 record_call_audit）
- Test: `gateway-proxy/tests/test_permission_middleware.py`

**Interfaces:**
- Consumes: Task 1 的 `audit.record_call(meta, status, error_type)`、`routing.resolve_target`、现有 `record_call_failure`
- Produces: `record_call_audit(token_info, mcp_name, latency_ms, trace_id, status, error_type=None) -> None`（构造 meta + 调 record_call）

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_permission_middleware.py`）

```python
# ─── on_call_tool 写 audit:calls ─────────────────────────────────

async def test_call_success_writes_audit_calls(fake_redis, monkeypatch):
    """成功调用 -> audit:calls 一条 status=ok。"""
    from auth import hash_token
    await fake_redis.hset(
        f"tokens:{hash_token('tok_ok')}",
        mapping={"id": "t1", "name": "caller",
                 "permissions": '{"zabbix": {"read": true, "write": false}}'},
    )
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_ok"},
    )

    async def call_next(ctx):
        return "result"

    mw = PermissionMiddleware()
    await mw.on_call_tool(FakeContext("zabbix_list_active_problems"), call_next)

    entries = await fake_redis.xrevrange("audit:calls", count=10)
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["status"] == "ok"
    assert fields["server"] == "zabbix"
    assert fields["tool"] == "list_active_problems"
    assert fields["token_name"] == "caller"


async def test_call_denied_writes_audit_calls_fail(fake_redis, monkeypatch):
    """权限拒绝 -> audit:calls 一条 status=fail + error_type（且 audit:failures 仍写）。"""
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {},  # 无 token -> invalid_token
    )
    mw = PermissionMiddleware()
    from fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        await mw.on_call_tool(FakeContext("zabbix_list_active_problems"), lambda ctx: "x")

    calls = await fake_redis.xrevrange("audit:calls", count=10)
    assert len(calls) == 1
    _, cfields = calls[0]
    assert cfields["status"] == "fail"
    assert cfields["error_type"] == "invalid_token"
    # failures 流仍写（现有失败面板依赖，不破坏）
    failures = await fake_redis.xrevrange("audit:failures", count=10)
    assert len(failures) == 1


async def test_call_backend_exception_writes_audit_calls_fail(fake_redis, monkeypatch):
    """后端异常 -> audit:calls status=fail + failures 流双写。"""
    from auth import hash_token
    await fake_redis.hset(
        f"tokens:{hash_token('tok_rw')}",
        mapping={"id": "t2", "name": "caller2",
                 "permissions": '{"zabbix": {"read": true, "write": true}}'},
    )
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_rw"},
    )

    async def call_next(ctx):
        raise RuntimeError("backend down")

    mw = PermissionMiddleware()
    with pytest.raises(RuntimeError):
        await mw.on_call_tool(FakeContext("zabbix_create_maintenance"), call_next)

    calls = await fake_redis.xrevrange("audit:calls", count=10)
    _, cfields = calls[0]
    assert cfields["status"] == "fail"
    assert cfields["error_type"] == "upstream_error"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway-proxy && uv run python -m pytest tests/test_permission_middleware.py -v -k audit_calls`
Expected: FAIL（audit:calls 流为空，断言 len==1 失败）

- [ ] **Step 3: 实现 record_call_audit**（追加到 `gateway-proxy/middleware.py`，紧接 record_call_failure 之后）

```python
async def record_call_audit(
    token_info: dict | None,
    mcp_name: str,
    latency_ms: int,
    trace_id: str,
    status: str,
    error_type: str | None = None,
) -> None:
    """写全量调用明细到 audit:calls（成功+失败均写）。

    与 record_call_failure 互补：failures 流供失败面板，calls 流供请求日志页。
    失败条目双写（failures + calls），成功仅写 calls。
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
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        status=status,
        error_type=error_type,
    )
```

middleware.py 顶部补 import：`from audit import record_call`（现有已有 `from audit import record_failure`，改为 `from audit import record_failure, record_call`）。

- [ ] **Step 4: on_call_tool 三路径接线**（`permission_middleware.py`）

顶部 import 改：`from middleware import check_call_permission, classify_error, record_call_failure, record_call_audit`

4a. **拒绝路径**（`if not allowed:` 块内，`raise ToolError` 之前，紧接 record_call_failure 之后）加：

```python
            await record_call_audit(
                token_info=token_info, mcp_name=tool_name,
                latency_ms=latency_ms, trace_id=trace_id,
                status="fail", error_type=error_type,
            )
```

4b. **异常路径**（`except Exception as exc:` 块内，`raise` 之前，紧接 record_call_failure 之后）加：

```python
            await record_call_audit(
                token_info=token_info, mcp_name=tool_name,
                latency_ms=latency_ms, trace_id=trace_id,
                status="fail", error_type=err_type,
            )
```

4c. **成功路径**（`return result` 之前）加：

```python
        await record_call_audit(
            token_info=token_info, mcp_name=tool_name,
            latency_ms=latency_ms, trace_id=trace_id,
            status="ok",
        )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd gateway-proxy && uv run python -m pytest tests/test_permission_middleware.py -v -k audit_calls`
Expected: PASS（3 个）

- [ ] **Step 6: 全量回归**

Run: `cd gateway-proxy && uv run python -m pytest tests/ -q`
Expected: 全过（现有 on_call_tool 测试无回归；新 record_call_audit 是旁路 await，若 Redis 异常不阻断--现有测试不应受影响）

- [ ] **Step 7: Commit**

```bash
git add gateway-proxy/middleware.py gateway-proxy/permission_middleware.py gateway-proxy/tests/test_permission_middleware.py
git commit -m "feat(gateway-proxy): on_call_tool records all calls to audit:calls (success+fail)"
```

---

### Task 3: admin /api/calls + 测试

**Files:**
- Create: `gateway-admin/api/calls.py`
- Modify: `gateway-admin/app.py`（注册 router）
- Test: `gateway-admin/tests/test_calls.py`

**Interfaces:**
- Consumes: `redis_client.get_redis()`、`auth.require_admin`
- Produces: `GET /api/calls?server=&status=&limit=&offset=` -> `{count, data:[{trace,server,tool,op,token_name,latency_ms,status,error_type,time}]}`

- [ ] **Step 1: 写失败测试**（`tests/test_calls.py`）

```python
"""Tests for /api/calls - 请求明细（audit:calls 流）。"""
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def test_list_calls_empty(client, auth_headers):
    """空流 -> count 0, data []。"""
    resp = client.get("/api/calls", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["data"] == []


def test_list_calls_returns_entries(client, fake_redis, auth_headers):
    """写入的条目按倒序返回。"""
    import json
    fake_redis.xadd("audit:calls", {
        "trace": "t1", "server": "tavily-mcp", "tool": "tavily_search",
        "op": "read", "token_name": "tok-a", "latency_ms": "42",
        "status": "ok", "error_type": "", "time": "2026-08-04T10:00:00Z",
    })
    fake_redis.xadd("audit:calls", {
        "trace": "t2", "server": "serpapi-mcp", "tool": "serpapi_baidu",
        "op": "read", "token_name": "tok-b", "latency_ms": "5",
        "status": "fail", "error_type": "upstream_error", "time": "2026-08-04T10:00:01Z",
    })
    resp = client.get("/api/calls", headers=auth_headers)
    body = resp.json()
    assert body["count"] == 2
    # 倒序：最新（t2）在前
    assert body["data"][0]["trace"] == "t2"
    assert body["data"][0]["status"] == "fail"
    assert body["data"][1]["trace"] == "t1"
    assert body["data"][1]["latency_ms"] == 42


def test_list_calls_filter_by_server(client, fake_redis, auth_headers):
    """server 过滤。"""
    fake_redis.xadd("audit:calls", {"trace": "t1", "server": "tavily-mcp", "tool": "t",
        "op": "read", "token_name": "n", "latency_ms": "1", "status": "ok", "error_type": "", "time": "x"})
    fake_redis.xadd("audit:calls", {"trace": "t2", "server": "brave-mcp", "tool": "t",
        "op": "read", "token_name": "n", "latency_ms": "1", "status": "ok", "error_type": "", "time": "x"})
    resp = client.get("/api/calls?server=tavily-mcp", headers=auth_headers)
    body = resp.json()
    assert body["count"] == 1
    assert body["data"][0]["server"] == "tavily-mcp"


def test_list_calls_filter_by_status(client, fake_redis, auth_headers):
    """status 过滤（只看失败）。"""
    fake_redis.xadd("audit:calls", {"trace": "t1", "server": "s", "tool": "t",
        "op": "read", "token_name": "n", "latency_ms": "1", "status": "ok", "error_type": "", "time": "x"})
    fake_redis.xadd("audit:calls", {"trace": "t2", "server": "s", "tool": "t",
        "op": "read", "token_name": "n", "latency_ms": "1", "status": "fail", "error_type": "perm", "time": "x"})
    resp = client.get("/api/calls?status=fail", headers=auth_headers)
    body = resp.json()
    assert body["count"] == 1
    assert body["data"][0]["status"] == "fail"


def test_list_calls_requires_auth(client):
    """无 token -> 401。"""
    resp = client.get("/api/calls")
    assert resp.status_code == 401
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway-admin && uv run python -m pytest tests/test_calls.py -v`
Expected: FAIL（404 路由不存在）

- [ ] **Step 3: 实现 api/calls.py**（镜像 dashboard.py 的 list_failures）

```python
"""请求明细 API：读 audit:calls Redis Stream（全量 tools/call，成功+失败）。

与 /api/failures 互补：failures 只含失败，calls 含全部。前端「请求日志」页用。
"""
from fastapi import APIRouter, Depends, Query

from auth import require_admin
from redis_client import get_redis

router = APIRouter(prefix="/api/calls", tags=["calls"])


@router.get("")
async def list_calls(
    server: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: str = Depends(require_admin),
):
    """列出调用明细，倒序（最新在前）。可按 server/status 过滤。"""
    r = get_redis()
    # XREVRANGE 倒序；多读 offset+limit 再切片（流无原生 offset）
    entries = await r.xrevrange("audit:calls", count=offset + limit)
    entries = entries[offset:]
    out = []
    for _id, fields in entries:
        rec = {
            "trace": fields.get("trace", ""),
            "server": fields.get("server", ""),
            "tool": fields.get("tool", ""),
            "op": fields.get("op", ""),
            "token_name": fields.get("token_name", ""),
            "latency_ms": int(fields["latency_ms"]) if fields.get("latency_ms", "").isdigit() else 0,
            "status": fields.get("status", ""),
            "error_type": fields.get("error_type", "") or None,
            "time": fields.get("time", ""),
        }
        if server is not None and rec["server"] != server:
            continue
        if status is not None and rec["status"] != status:
            continue
        out.append(rec)
    return {"count": len(out), "data": out}
```

- [ ] **Step 4: 注册 router**（`gateway-admin/app.py`）

```python
from api import servers, tokens, dashboard, keys, calls
app.include_router(servers.router)
app.include_router(tokens.router)
app.include_router(dashboard.router)
app.include_router(keys.router)
app.include_router(calls.router)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd gateway-admin && uv run python -m pytest tests/test_calls.py -v`
Expected: PASS（5 个）

- [ ] **Step 6: 全量回归**

Run: `cd gateway-admin && uv run python -m pytest tests/ -q`
Expected: 全过

- [ ] **Step 7: Commit**

```bash
git add gateway-admin/api/calls.py gateway-admin/app.py gateway-admin/tests/test_calls.py
git commit -m "feat(gateway-admin): /api/calls endpoint for call audit stream"
```

---

### Task 4: admin 前端「请求日志」页

**Files:**
- Create: `gateway-admin/admin-ui/src/views/Calls.vue`
- Modify: `gateway-admin/admin-ui/src/api/index.js`、`router/index.js`、`components/Sidebar.vue`

**Interfaces:**
- Consumes: Task 3 的 `GET /api/calls`
- Produces: `/calls` 页面（表格 + 过滤 + 分页）

- [ ] **Step 1: api/index.js 加函数**（追加到搜索 keys 函数后）

```javascript
// ── 请求日志（call audit） ──────────────────────
export function getCalls(params = {}) {
  const p = new URLSearchParams()
  if (params.server) p.set('server', params.server)
  if (params.status) p.set('status', params.status)
  if (params.limit) p.set('limit', params.limit)
  if (params.offset) p.set('offset', params.offset)
  return apiFetch(`/api/calls?${p}`)
}
```

- [ ] **Step 2: 写 Calls.vue**（参考 APIKeys.vue 的表格/过滤模式）

```vue
<!-- src/views/Calls.vue - 请求日志（全量 tools/call 明细） -->
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
        <tr v-for="c in calls" :key="c.trace + c.time" :class="{ 'row-fail': c.status === 'fail' }">
          <td>{{ c.time }}</td>
          <td>{{ c.server }}</td>
          <td>{{ c.tool }}</td>
          <td>{{ c.token_name }}</td>
          <td>{{ c.op === 'write' ? '写' : '读' }}</td>
          <td>{{ c.latency_ms }}ms</td>
          <td>
            <span v-if="c.status === 'ok'" class="ok">✓</span>
            <span v-else class="fail">✗ {{ c.error_type }}</span>
          </td>
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

async function reload() {
  offset.value = 0
  await load()
}
async function load() {
  const body = await getCalls({ server: filterServer.value, status: filterStatus.value, limit, offset: offset.value })
  calls.value = body.data
}
function prev() { offset.value = Math.max(0, offset.value - limit); load() }
function next() { offset.value += limit; load() }
onMounted(reload)
</script>

<style scoped>
.filters { margin-bottom: 16px; display: flex; gap: 12px; align-items: center; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); font-size: 13px; }
.row-fail { background: rgba(255,90,90,0.06); }
.ok { color: #3fb950; }
.fail { color: #f85149; }
.empty { padding: 32px; text-align: center; color: var(--text-dim); }
.pager { margin-top: 16px; display: flex; gap: 8px; }
</style>
```

- [ ] **Step 3: router 加路由**（`router/index.js`）

```javascript
{ path: '/calls', name: 'calls', component: () => import('../views/Calls.vue') },
```

- [ ] **Step 4: Sidebar 加菜单项**（`components/Sidebar.vue` 的 navItems 数组，API Keys 之后）

```javascript
{ id: 'calls', label: '请求日志', icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 12h4M3 6h4M3 18h4M10 12h11M10 6h11M10 18h11"/></svg>' },
```

- [ ] **Step 5: 构建前端验证**

```bash
cd gateway-admin/admin-ui && npm run build
```
Expected: dist 生成无编译错误

- [ ] **Step 6: 手工冒烟（本地起 admin，可选）**

```bash
cd gateway-admin && REDIS_URL=redis://localhost:6379/0 JWT_SECRET=dev uv run uvicorn app:app --port 8081 &
# 浏览器访问 http://localhost:8081/calls -> 请求日志页（空数据时显示"暂无调用记录"）
```

- [ ] **Step 7: Commit**

```bash
git add gateway-admin/admin-ui/
git commit -m "feat(gateway-admin): 请求日志 page (Calls.vue) with filters + pagination"
```

---

## Self-Review 记录

**Spec 覆盖核对：**
- ✅ 全量调用明细（成功+失败）-> Task 1 record_call + Task 2 三路径接线
- ✅ 新流 audit:calls，保留 failures -> Task 1（独立流）+ Task 2 测试断言 failures 仍写
- ✅ 元数据字段 -> Task 1 record_call 字段（trace/server/tool/op/token_name/latency_ms/status/error_type/time）
- ✅ admin /api/calls -> Task 3
- ✅ 前端请求日志页 -> Task 4
- ✅ 不审计 tools/list/ping -> 只在 on_call_tool 接线（Task 2），on_list_tools 不动
- ✅ 审计旁路不阻断 -> Task 1 record_call try/except + Task 6 测试
- ✅ 聚合不持久化（不动 Prometheus）-> 无相关任务（非目标）

**类型一致性：**
- record_call(meta, status, error_type) -- Task 1 定义，Task 2 record_call_audit 调用一致
- record_call_audit(token_info, mcp_name, latency_ms, trace_id, status, error_type) -- Task 2 定义，on_call_tool 三路径调用一致
- /api/calls 返回 {count, data:[...]} -- Task 3 定义，Task 4 前端 body.data 消费一致

**YAGNI 简化（已注明）：** record_call 不含 journey 字段（请求日志页不展示，failures 流已有 journey 供失败追踪）

**坑位预判：**
1. middleware.py import：现有 `from audit import record_failure` 改为 `from audit import record_failure, record_call`
2. on_call_tool 成功路径：record_call_audit 在 `return result` 之前，latency_ms 已算出
3. fakeredis xadd 支持 maxlen 参数（验证：Task 1 MAXLEN 测试用小值 3 确认截断）
4. 前端 Calls.vue 复用 APIKeys.vue 的 CSS 变量（var(--border) 等），与现有主题一致
