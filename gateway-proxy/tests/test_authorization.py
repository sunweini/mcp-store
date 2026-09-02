"""Test for the authorize seam (authorization.py): pure function, data injected.

No global state / Redis / TOOL_REGISTRY needed — tool_modes is passed as data,
so the whole authorization decision is testable with plain dicts.
"""
import pytest

from authorization import (
    authorize,
    get_tool_modes,
    AuthResult,
    INVALID_TOKEN,
    INVALID_TARGET,
    PERMISSION_DENIED,
)

# 共享 fixture：zabbix 一个 read 一个 write，tavily 一个 read
_TOOL_MODES = {
    "zabbix": {"list_active_problems": "read", "create_maintenance": "write"},
    "tavily": {"tavily_search": "read"},
}

_SECRET = "permissions"  # 简化：非 None 表示有 token


# ─── invalid_token ─────────────────────────────────────────────────

def test_authorize_none_permissions_is_invalid_token():
    """permissions None（无有效 token）→ invalid_token，不解析名字。"""
    res = authorize(None, "zabbix_list_active_problems", _TOOL_MODES)
    assert res.allowed is False
    assert res.error_type == INVALID_TOKEN
    assert res.server == ""
    assert res.tool == ""


# ─── invalid_target ────────────────────────────────────────────────

def test_authorize_no_underscore_is_invalid_target():
    """工具名无命名空间前缀（无下划线）→ invalid_target。"""
    res = authorize(_SECRET, "rootlevel", _TOOL_MODES)
    assert res.allowed is False
    assert res.error_type == INVALID_TARGET


# ─── permission_denied ─────────────────────────────────────────────

def test_authorize_read_only_cannot_write():
    """token 只 read，调用 write 工具 → permission_denied，且解析出 server/tool/mode。"""
    perms = {"zabbix": {"read": True, "write": False}}
    res = authorize(perms, "zabbix_create_maintenance", _TOOL_MODES)
    assert res.allowed is False
    assert res.error_type == PERMISSION_DENIED
    assert res.server == "zabbix"
    assert res.tool == "create_maintenance"
    assert res.mode == "write"


def test_authorize_unknown_server_denied():
    """token 无该 server 权限 → permission_denied（server/tool 仍解析出）。

    split_prefix 按第一个下划线切分：server 名可含连字符（如 ghost-mcp 是
    合法 server 名），第一个 _ 之后是 tool。
    """
    perms = {"zabbix": {"read": True, "write": False}}
    res = authorize(perms, "ghost-mcp_web_search", _TOOL_MODES)
    assert res.allowed is False
    assert res.error_type == PERMISSION_DENIED
    assert res.server == "ghost-mcp"
    assert res.tool == "web_search"


def test_authorize_unknown_tool_mode_defaults_read():
    """tool_modes 里没有该工具 → mode 默认 read，仅当 token 有 read 才放行。

    未注册工具按历史语义退化 read（审计 op 不因 registry 缺失落空）。
    """
    perms = {"zabbix": {"read": True, "write": False}}
    res = authorize(perms, "zabbix_undeclared_tool", _TOOL_MODES)
    assert res.mode == "read"
    # 有 read -> 放行（退化为 read 的可访问性）
    assert res.allowed is True
    assert res.error_type is None


# ─── allowed ───────────────────────────────────────────────────────

def test_authorize_allows_read():
    perms = {"zabbix": {"read": True, "write": False}}
    res = authorize(perms, "zabbix_list_active_problems", _TOOL_MODES)
    assert res.allowed is True
    assert res.error_type is None
    assert res.server == "zabbix"
    assert res.tool == "list_active_problems"
    assert res.mode == "read"


def test_authorize_allows_write_when_write_granted():
    """token 有 write→read+write 隐式成立（写工具可调）。"""
    perms = {"zabbix": {"read": True, "write": True}}
    res = authorize(perms, "zabbix_create_maintenance", _TOOL_MODES)
    assert res.allowed is True
    assert res.mode == "write"


def test_authorize_write_only_can_write_not_read():
    """token 只 write → write 工具放行，read 工具拒绝。"""
    perms = {"zabbix": {"read": False, "write": True}}
    assert authorize(perms, "zabbix_create_maintenance", _TOOL_MODES).allowed is True
    assert authorize(perms, "zabbix_list_active_problems", _TOOL_MODES).allowed is False


def test_authorize_empty_permissions_denied():
    perms = {}
    res = authorize(perms, "zabbix_list_active_problems", _TOOL_MODES)
    assert res.allowed is False
    assert res.error_type == PERMISSION_DENIED


# ─── AuthResult dataclass ──────────────────────────────────────────

def test_auth_result_defaults():
    r = AuthResult(False, INVALID_TOKEN)
    assert r.server == ""
    assert r.tool == ""
    assert r.mode == "read"


# ─── get_tool_modes ────────────────────────────────────────────────
# 从 TOOL_REGISTRY 全局快照（授权模块唯一碰全局的边界），返回独立的 copy。

def test_get_tool_modes_snapshots_registry(monkeypatch):
    import routing
    routing.TOOL_REGISTRY["zabbix"] = {"list_active_problems": "read"}
    try:
        modes = get_tool_modes()
        assert modes["zabbix"]["list_active_problems"] == "read"
        # 返回快照副本：修改副本不影响全局
        modes["zabbix"]["list_active_problems"] = "write"
        assert routing.TOOL_REGISTRY["zabbix"]["list_active_problems"] == "read"
    finally:
        routing.TOOL_REGISTRY.pop("zabbix", None)
