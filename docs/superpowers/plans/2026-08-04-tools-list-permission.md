# tools/list 权限过滤实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** gateway-proxy 的 tools/list 按 token 权限动态过滤——匿名/无效 token 返回空清单，有效 token 只见其有 (server, mode) 权限的工具（工具级粒度：read-only token 看不到写工具）。

**Architecture:** 在现有 `PermissionMiddleware`（已拦 tools/call）加 FastMCP v4 原生 `on_list_tools` hook。纯组合现有组件：`_extract_token` + `verify_token`（token 验证）、`resolve_target`（namespace → server/tool/mode）、`check_permission`（权限判定）。零新权限逻辑。

**Tech Stack:** FastMCP 4.0.0b1（Middleware.on_list_tools）、fakeredis（测试）、pytest-asyncio。

## Global Constraints

- 匿名/无效 token → tools/list 返回**空清单**（不报错，不抛异常）
- 工具级过滤：工具可见 ⇔ `check_permission(token_info, server, mode)` 为 True
- mode 来源 TOOL_REGISTRY（registry.py 探活时按 destructiveHint 分类存储），与 tools/call 判定同源
- 未注册 server 前缀的工具（resolve_target 抛 UnknownServerError）→ 跳过不列出
- tools/call 行为不变（现有测试必须全过）
- ping/initialize 不动（匿名可探活）
- permission_middleware.py 模块与类 docstring 中"tools/list pass through"/"Only tools/call is intercepted"表述必须更新
- 结构化日志 structlog（若加日志）；注释写"为什么"

---

## 文件结构

| 文件 | 改动 |
|---|---|
| `gateway-proxy/permission_middleware.py` | 加 `on_list_tools` 方法 + import 补充 + docstring 更新 |
| `gateway-proxy/tests/test_permission_middleware.py` | 加 8 个 on_list_tools 测试 |

既有测试模式（已核实，直接照用）：
- fixture `register_zabbix_tools`（autouse）：`register_tools("zabbix", [{"name": "list_active_problems", "mode": "read"}, {"name": "create_maintenance", "mode": "write"}])`，teardown `clear_tools("zabbix")`
- fixture `fake_redis`：fakeredis 替代真 Redis
- token 种法：`await fake_redis.hset(f"tokens:{hash_token('tok_x')}", mapping={"id": ..., "name": ..., "permissions": '{"zabbix": {"read": true, "write": false}}'})`
- header mock：`monkeypatch.setattr("permission_middleware.get_http_headers", lambda include=None: {"authorization": "Bearer tok_x"})`

---

### Task 1: on_list_tools 权限过滤

**Files:**
- Modify: `gateway-proxy/permission_middleware.py`（加方法 + import + docstring）
- Test: `gateway-proxy/tests/test_permission_middleware.py`（加 8 测试）

**Interfaces:**
- Consumes: `_extract_token(headers)`、`auth.verify_token(token) -> dict | None`（返回 `{"id","name","permissions":{server:{read,write}}}`）、`routing.resolve_target(mcp_name) -> (server, tool, mode)`（未注册抛 `UnknownServerError`）、`auth.check_permission(token_info, server, mode) -> bool`
- Produces: `PermissionMiddleware.on_list_tools(context, call_next) -> list`——过滤后的工具列表

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_permission_middleware.py` 末尾）

```python
# ─── PermissionMiddleware.on_list_tools: permission filtering ─────


class FakeTool:
    """Simulates FastMCP Tool — on_list_tools only reads .name."""
    def __init__(self, name):
        self.name = name


def _full_tool_list():
    """The gateway's mounted tool list: zabbix read+write tools."""
    return [
        FakeTool("zabbix_list_active_problems"),   # read
        FakeTool("zabbix_create_maintenance"),     # write
    ]


async def _seed_token(fake_redis, tok: str, permissions: dict):
    import json
    from auth import hash_token
    await fake_redis.hset(
        f"tokens:{hash_token(tok)}",
        mapping={
            "id": "tok_id",
            "name": "test-token",
            "permissions": json.dumps(permissions),
        },
    )


async def test_list_tools_anonymous_returns_empty(fake_redis, monkeypatch):
    """无 Authorization header → 空清单。"""
    monkeypatch.setattr("permission_middleware.get_http_headers", lambda include=None: {})
    mw = PermissionMiddleware()
    result = await mw.on_list_tools(FakeContext("x"), lambda ctx: _full_tool_list())
    assert result == []


async def test_list_tools_invalid_token_returns_empty(fake_redis, monkeypatch):
    """token 不在 Redis → 空清单。"""
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_unknown"},
    )
    mw = PermissionMiddleware()
    result = await mw.on_list_tools(FakeContext("x"), lambda ctx: _full_tool_list())
    assert result == []


async def test_list_tools_read_only_sees_only_read_tools(fake_redis, monkeypatch):
    """zabbix 只 read → 只见读工具，写工具缺席。"""
    await _seed_token(fake_redis, "tok_read", {"zabbix": {"read": True, "write": False}})
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_read"},
    )
    mw = PermissionMiddleware()
    result = await mw.on_list_tools(FakeContext("x"), lambda ctx: _full_tool_list())
    names = [t.name for t in result]
    assert "zabbix_list_active_problems" in names
    assert "zabbix_create_maintenance" not in names


async def test_list_tools_write_only_sees_only_write_tools(fake_redis, monkeypatch):
    """zabbix 只 write → 只见写工具。"""
    await _seed_token(fake_redis, "tok_write", {"zabbix": {"read": False, "write": True}})
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_write"},
    )
    mw = PermissionMiddleware()
    result = await mw.on_list_tools(FakeContext("x"), lambda ctx: _full_tool_list())
    names = [t.name for t in result]
    assert "zabbix_create_maintenance" in names
    assert "zabbix_list_active_problems" not in names


async def test_list_tools_read_write_sees_all(fake_redis, monkeypatch):
    """read+write → 全见。"""
    await _seed_token(fake_redis, "tok_rw", {"zabbix": {"read": True, "write": True}})
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_rw"},
    )
    mw = PermissionMiddleware()
    result = await mw.on_list_tools(FakeContext("x"), lambda ctx: _full_tool_list())
    assert len(result) == 2


async def test_list_tools_multi_server_mixed_permissions(fake_redis, monkeypatch):
    """多 server 混合：各自按权限过滤后合并。"""
    register_tools("tavily", [{"name": "tavily_search", "mode": "read"}])
    try:
        await _seed_token(fake_redis, "tok_mix", {
            "zabbix": {"read": True, "write": False},
            "tavily": {"read": True, "write": True},
        })
        monkeypatch.setattr(
            "permission_middleware.get_http_headers",
            lambda include=None: {"authorization": "Bearer tok_mix"},
        )
        tools = _full_tool_list() + [FakeTool("tavily_tavily_search")]
        mw = PermissionMiddleware()
        result = await mw.on_list_tools(FakeContext("x"), lambda ctx: tools)
        names = {t.name for t in result}
        assert names == {"zabbix_list_active_problems", "tavily_tavily_search"}
    finally:
        clear_tools("tavily")


async def test_list_tools_unregistered_prefix_skipped(fake_redis, monkeypatch):
    """未注册 server 前缀的工具 → 跳过不列出（全权限 token 也看不到）。"""
    await _seed_token(fake_redis, "tok_all", {"zabbix": {"read": True, "write": True}})
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_all"},
    )
    tools = _full_tool_list() + [FakeTool("ghost_some_tool")]
    mw = PermissionMiddleware()
    result = await mw.on_list_tools(FakeContext("x"), lambda ctx: tools)
    names = {t.name for t in result}
    assert "ghost_some_tool" not in names
    assert len(result) == 2


async def test_list_tools_empty_permissions_returns_empty(fake_redis, monkeypatch):
    """token 无任何 server 权限 → 空清单。"""
    await _seed_token(fake_redis, "tok_empty", {})
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_empty"},
    )
    mw = PermissionMiddleware()
    result = await mw.on_list_tools(FakeContext("x"), lambda ctx: _full_tool_list())
    assert result == []
```

注意：测试用到文件顶部已有的 `register_tools`/`clear_tools` import（`from routing import register_tools, clear_tools`）与 `FakeContext`，均已存在无需新增。

- [ ] **Step 2: 运行确认失败**

```bash
cd gateway-proxy && uv run python -m pytest tests/test_permission_middleware.py -v -k list_tools
```
Expected: FAIL——`PermissionMiddleware` 无 `on_list_tools`（AttributeError）

- [ ] **Step 3: 实现 on_list_tools**（`gateway-proxy/permission_middleware.py`）

3a. import 补充（文件顶部 import 区）：

```python
from auth import verify_token, check_permission
from routing import resolve_target, UnknownServerError
```

（原 `from auth import verify_token` 改为上面一行；`check_permission` 定义在 auth.py，`resolve_target`/`UnknownServerError` 在 routing.py——已核实位置）

3b. `PermissionMiddleware` 类内加方法：

```python
    async def on_list_tools(self, context: MiddlewareContext, call_next):
        """Filter tools/list by token permissions (tool-level granularity).

        Anonymous/invalid token -> empty list. Otherwise a tool is visible
        iff the token grants its (server, mode). Mode comes from the same
        TOOL_REGISTRY that tools/call checks, so list visibility and call
        permission never diverge.
        """
        tools = await call_next(context)

        token = _extract_token(get_http_headers())
        token_info = await verify_token(token) if token else None
        if token_info is None:
            # 空清单而非报错：client 能连通但看不到工具，不泄露名称/描述，
            # 也避免 list 报错引发 client 断连/重试风暴
            return []

        visible = []
        for t in tools:
            try:
                server, _tool, mode = resolve_target(t.name)
            except UnknownServerError:
                # 未注册前缀：来源不确定，安全默认不列出
                continue
            if check_permission(token_info, server, mode):
                visible.append(t)
        return visible
```

3c. docstring 更新——模块 docstring（文件头）把：

```
Non-tools/call requests (tools/list, ping, initialize) pass through untouched -
the gateway returns an empty tool list for unauthenticated clients, which is
by design: clients need to discover the gateway before they can call anything.
```

改为：

```
tools/list is filtered by token permissions (on_list_tools): anonymous or
invalid tokens get an empty list; valid tokens see only the tools their
(server, mode) permissions grant. ping and initialize still pass through
untouched so health checks work without auth.
```

类 docstring 把：

```
Only tools/call is intercepted - tools/list and other methods pass through
without auth so clients can discover the gateway before authenticating.
```

改为：

```
Intercepts tools/call (auth + permission + audit) and filters tools/list
by token permissions. ping/initialize pass through for unauthenticated
health checks.
```

- [ ] **Step 4: 跑新测试确认通过**

```bash
cd gateway-proxy && uv run python -m pytest tests/test_permission_middleware.py -v -k list_tools
```
Expected: PASS（8 个）

- [ ] **Step 5: 全量回归**

```bash
cd gateway-proxy && uv run python -m pytest tests/ -q
```
Expected: 全过（尤其 tools/call 相关测试不回归）。若 `smoke_test.py` 需要真实 Redis 被 skip，属正常。

- [ ] **Step 6: 端到端冒烟（本地，可选——需 Redis）**

```bash
# Redis 未起则: redis-server --daemonize yes
cd gateway-proxy && REDIS_URL=redis://localhost:6379/0 GATEWAY_PORT=8082 uv run python server.py > /tmp/proxy-smoke.log 2>&1 &
sleep 4
# 匿名 tools/list → 应空清单
curl -s -X POST http://localhost:8082/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | grep -o '"tools":\[\]' && echo "anonymous empty: OK"
# 清理
pkill -f "gateway-proxy.*server.py"
```

Expected: `anonymous empty: OK`（Redis 不可用则跳过本步，单测已覆盖）

- [ ] **Step 7: Commit**

```bash
git add gateway-proxy/permission_middleware.py gateway-proxy/tests/test_permission_middleware.py
git commit -m "feat(gateway-proxy): filter tools/list by token permissions (tool-level visibility)"
```

---

## Self-Review 记录

**Spec 覆盖核对：**
- ✅ 匿名/无效 → 空清单（test 1/2）
- ✅ 工具级过滤 read-only/write-only/read+write（test 3/4/5）
- ✅ 多 server 混合（test 6）
- ✅ 未注册前缀跳过（test 7）
- ✅ 空 permissions（test 8）
- ✅ tools/call 不回归（Step 5 全量）
- ✅ ping/initialize 不动（实现未触及，spec 关键点 6）
- ✅ docstring 更新（Step 3c）
- ✅ import 来源核实（auth.py: check_permission；routing.py: resolve_target/UnknownServerError）
- ✅ 部署影响：只改 gateway-proxy，重建容器生效（无代码任务，部署时执行）

**坑位预判（实施注意）：**
1. `call_next` 返回值：FastMCP on_list_tools 的 call_next 返回 `list[Tool]`——测试用 lambda 返回 FakeTool 列表模拟即可；生产环境 FastMCP 传真实 Tool 对象，`t.name` 属性存在
2. `get_http_headers()` 无参调用（生产签名 `get_http_headers()`；monkeypatch 的 lambda 带 `include=None` 默认参数兼容两种调用法——照现有测试写法）
3. `verify_token` 是 async（`await`）
4. fake_redis fixture 已 monkeypatch `redis_client._redis`——verify_token 走 get_redis() 读到 fake，无需额外 mock
