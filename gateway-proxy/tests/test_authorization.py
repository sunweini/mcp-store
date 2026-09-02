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
    UNKNOWN_MODE,
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
    """token 有权限但 server 未注册（tool_modes 无记录）→ unknown_mode。

    split_prefix 按第一个下划线切分：server 名可含连字符（如 ghost-mcp 是
    合法 server 名），第一个 _ 之后是 tool。mode 未知 → fail-closed 拒绝，
    与"有 mode 但权限不够"（permission_denied）区分开。
    """
    perms = {"ghost-mcp": {"read": True, "write": True}}
    res = authorize(perms, "ghost-mcp_web_search", _TOOL_MODES)
    assert res.allowed is False
    assert res.error_type == UNKNOWN_MODE
    assert res.server == "ghost-mcp"
    assert res.tool == "web_search"
    assert res.mode is None


def test_authorize_known_server_without_grant_denied():
    """server 已知（tool_modes 有记录）但 token 无权限 → permission_denied。"""
    perms = {"tavily": {"read": False, "write": False}}
    res = authorize(perms, "tavily_tavily_search", _TOOL_MODES)
    assert res.allowed is False
    assert res.error_type == PERMISSION_DENIED
    assert res.mode == "read"


def test_authorize_unknown_tool_mode_denied():
    """tool_modes 里没有该工具 → fail-closed：deny + unknown_mode。

    fail-closed 语义：mode 未知（未注册/未 introspect）时拒绝访问，不再
    退化成默认 read 放行（防只读 token 越权访问未知 write 工具）。审计
    借此区分"工具名畸形"（invalid_target）与"mode 未同步"（unknown_mode）。
    """
    perms = {"zabbix": {"read": True, "write": True}}
    res = authorize(perms, "zabbix_undeclared_tool", _TOOL_MODES)
    assert res.allowed is False
    assert res.error_type == UNKNOWN_MODE
    # server/tool 仍解析出（split_prefix 纯切分），mode 未知为 None
    assert res.server == "zabbix"
    assert res.tool == "undeclared_tool"
    assert res.mode is None


def test_authorize_unknown_server_mode_denied():
    """server 在 tool_modes 里无记录 → deny + unknown_mode（而非按 read 判定）。"""
    perms = {"ghost-mcp": {"read": True, "write": True}}
    res = authorize(perms, "ghost-mcp_web_search", _TOOL_MODES)
    assert res.allowed is False
    assert res.error_type == UNKNOWN_MODE
    assert res.mode is None


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
    """默认 mode 为 None（fail-closed：未知即拒绝，不伪装成 read）。"""
    r = AuthResult(False, INVALID_TOKEN)
    assert r.server == ""
    assert r.tool == ""
    assert r.mode is None


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
