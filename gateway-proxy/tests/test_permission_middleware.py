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
