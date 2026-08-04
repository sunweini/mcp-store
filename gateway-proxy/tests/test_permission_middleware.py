"""Tests for the PermissionMiddleware (FastMCP Middleware subclass).

Mocks call_next to simulate the backend; verifies that allowed calls pass
through and denied calls raise ToolError + write an audit record.
"""
import pytest

from fastmcp.exceptions import ToolError

from permission_middleware import PermissionMiddleware, _extract_token, _current_trace_id
from routing import register_tools, clear_tools


@pytest.fixture(autouse=True)
def register_zabbix_tools():
    """Register zabbix server with read + write tools so resolve_target works."""
    register_tools("zabbix", [
        {"name": "list_active_problems", "mode": "read"},
        {"name": "create_maintenance", "mode": "write"},
    ])
    yield
    clear_tools("zabbix")


class FakeMessage:
    """Simulates CallToolRequestParams with .name and .arguments."""
    def __init__(self, name, arguments=None):
        self.name = name
        self.arguments = arguments or {}


class FakeContext:
    """Simulates MiddlewareContext with .message and .fastmcp_context."""
    def __init__(self, tool_name):
        self.message = FakeMessage(tool_name)
        self.fastmcp_context = None
        self.method = "tools/call"
        self.source = "client"
        self.type = "request"


# ─── _extract_token ────────────────────────────────────────────────

def test_extract_token_valid_bearer():
    headers = {"authorization": "Bearer my_secret_token"}
    assert _extract_token(headers) == "my_secret_token"


def test_extract_token_missing_header():
    assert _extract_token({}) is None
    assert _extract_token(None) is None


def test_extract_token_wrong_scheme():
    headers = {"authorization": "Basic abc123"}
    assert _extract_token(headers) is None


def test_extract_token_case_insensitive_prefix():
    # The header key is lowercased by get_http_headers; the scheme may be
    # any case from the client.
    headers = {"authorization": "bearer tok"}
    assert _extract_token(headers) == "tok"


# ─── _current_trace_id ─────────────────────────────────────────────

def test_current_trace_id_returns_nonempty_string():
    tid = _current_trace_id()
    assert isinstance(tid, str)
    assert len(tid) > 0


# ─── PermissionMiddleware: allowed call passes through ────────────

async def test_middleware_allows_authorized_read(fake_redis, monkeypatch):
    """Token with zabbix:read calling a read tool -> call_next is invoked."""
    # Seed the token in Redis so verify_token succeeds.
    from auth import hash_token
    await fake_redis.hset(
        f"tokens:{hash_token('tok_read')}",
        mapping={
            "id": "tok_1",
            "name": "reader",
            "permissions": '{"zabbix": {"read": true, "write": false}}',
        },
    )

    # Mock get_http_headers to return our Bearer token.
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_read"},
    )

    called = False

    async def call_next(ctx):
        nonlocal called
        called = True
        return "tool_result"

    mw = PermissionMiddleware()
    ctx = FakeContext("zabbix_list_active_problems")
    result = await mw.on_call_tool(ctx, call_next)

    assert called is True
    assert result == "tool_result"


# ─── PermissionMiddleware: denied call raises ToolError ───────────

async def test_middleware_denies_missing_token(fake_redis, monkeypatch):
    """No Authorization header -> invalid_token -> ToolError."""
    monkeypatch.setattr("permission_middleware.get_http_headers", lambda include=None: {})

    async def call_next(ctx):
        pytest.fail("call_next must not be reached when denied")

    mw = PermissionMiddleware()
    ctx = FakeContext("zabbix_list_active_problems")

    with pytest.raises(ToolError):
        await mw.on_call_tool(ctx, call_next)

    # Verify an audit failure was written.
    entries = await fake_redis.xrange("audit:failures")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["error_type"] == "invalid_token"
    assert fields["token_name"] == "(anonymous)"


async def test_middleware_denies_wrong_permission(fake_redis, monkeypatch):
    """Token has read-only but calls a write tool -> permission_denied."""
    from auth import hash_token
    await fake_redis.hset(
        f"tokens:{hash_token('tok_ro')}",
        mapping={
            "id": "tok_2",
            "name": "readonly",
            "permissions": '{"zabbix": {"read": true, "write": false}}',
        },
    )
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_ro"},
    )

    async def call_next(ctx):
        pytest.fail("call_next must not be reached when denied")

    mw = PermissionMiddleware()
    ctx = FakeContext("zabbix_create_maintenance")

    with pytest.raises(ToolError):
        await mw.on_call_tool(ctx, call_next)

    entries = await fake_redis.xrange("audit:failures")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["error_type"] == "permission_denied"
    assert fields["token_name"] == "readonly"


# ─── PermissionMiddleware: verify_token exception guard ──────────

async def test_middleware_verify_token_exception_treated_as_invalid(fake_redis, monkeypatch):
    """Malformed Redis data (KeyError/JSONDecodeError) -> invalid_token, not crash."""
    # Don't seed any token data; instead make verify_token raise.
    async def boom_verify(token):
        raise KeyError("corrupted")

    monkeypatch.setattr("permission_middleware.verify_token", boom_verify)
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer any_token"},
    )

    async def call_next(ctx):
        pytest.fail("call_next must not be reached when token is invalid")

    mw = PermissionMiddleware()
    ctx = FakeContext("zabbix_list_active_problems")

    with pytest.raises(ToolError):
        await mw.on_call_tool(ctx, call_next)

    entries = await fake_redis.xrange("audit:failures")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["error_type"] == "invalid_token"


# ─── PermissionMiddleware: backend exception records failure ──────

async def test_middleware_records_backend_failure(fake_redis, monkeypatch):
    """When call_next raises, the middleware classifies + records the error."""
    from auth import hash_token
    await fake_redis.hset(
        f"tokens:{hash_token('tok_rw')}",
        mapping={
            "id": "tok_3",
            "name": "readwrite",
            "permissions": '{"zabbix": {"read": true, "write": true}}',
        },
    )
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_rw"},
    )

    import httpx

    async def call_next(ctx):
        raise httpx.TimeoutException("upstream timed out")

    mw = PermissionMiddleware()
    ctx = FakeContext("zabbix_list_active_problems")

    with pytest.raises(httpx.TimeoutException):
        await mw.on_call_tool(ctx, call_next)

    entries = await fake_redis.xrange("audit:failures")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["error_type"] == "upstream_timeout"
    assert fields["token_name"] == "readwrite"


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


# ─── on_list_tools: review fixes (C1 header / I1 guard / I2 ValueError / M1 async) ───


def _make_http_request(headers: dict[str, str]):
    """Minimal starlette Request carrying only the given headers."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


async def test_list_tools_real_http_context_authorization_passes(fake_redis):
    """不 monkeypatch get_http_headers：真实 fastmcp 函数 + 真实 HTTP 上下文。

    fastmcp 的 get_http_headers 默认排除 authorization——on_list_tools 必须传
    include={"authorization"} 才能取到 token。若有人删掉 include 参数，
    有效 token 会退化为空清单，本测试失败（C1 回归防线）。
    """
    from fastmcp.server.http import _current_http_request

    await _seed_token(fake_redis, "tok_real", {"zabbix": {"read": True, "write": True}})
    cv_token = _current_http_request.set(
        _make_http_request({"authorization": "Bearer tok_real"})
    )
    try:
        mw = PermissionMiddleware()
        result = await mw.on_list_tools(FakeContext("x"), lambda ctx: _full_tool_list())
        assert len(result) == 2
    finally:
        _current_http_request.reset(cv_token)


async def test_list_tools_async_call_next(fake_redis, monkeypatch):
    """生产 FastMCP 的 call_next 是 async——覆盖 inspect.isawaitable 的 await 分支。"""
    await _seed_token(fake_redis, "tok_async", {"zabbix": {"read": True, "write": True}})
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_async"},
    )

    async def call_next(ctx):
        return _full_tool_list()

    mw = PermissionMiddleware()
    result = await mw.on_list_tools(FakeContext("x"), call_next)
    assert len(result) == 2


async def test_list_tools_verify_token_exception_returns_empty(fake_redis, monkeypatch):
    """verify_token 抛异常（Redis 脏数据）→ 空清单而非异常，对齐 on_call_tool 防护。"""
    async def boom_verify(token):
        raise KeyError("corrupted")

    monkeypatch.setattr("permission_middleware.verify_token", boom_verify)
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer any_token"},
    )
    mw = PermissionMiddleware()
    result = await mw.on_list_tools(FakeContext("x"), lambda ctx: _full_tool_list())
    assert result == []


async def test_list_tools_no_underscore_tool_skipped(fake_redis, monkeypatch):
    """无下划线前缀的工具名 → split_prefix 抛 ValueError → 跳过，不能 500。"""
    await _seed_token(fake_redis, "tok_noprefix", {"zabbix": {"read": True, "write": True}})
    monkeypatch.setattr(
        "permission_middleware.get_http_headers",
        lambda include=None: {"authorization": "Bearer tok_noprefix"},
    )
    tools = _full_tool_list() + [FakeTool("rootlevel")]
    mw = PermissionMiddleware()
    result = await mw.on_list_tools(FakeContext("x"), lambda ctx: tools)
    names = {t.name for t in result}
    assert names == {"zabbix_list_active_problems", "zabbix_create_maintenance"}
