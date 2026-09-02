"""Tests for namespace prefix routing + tool mode registry."""
import pytest
from routing import split_prefix, register_tools, clear_tools, get_tool_mode, resolve_target, UnknownServerError


def test_split_prefix_basic():
    assert split_prefix("zabbix_list_active_problems") == ("zabbix", "list_active_problems")


def test_split_prefix_hyphenated_server():
    # server name may contain hyphens; first _ is the separator
    assert split_prefix("my-db_run_query") == ("my-db", "run_query")


def test_split_prefix_no_underscore_raises():
    with pytest.raises(ValueError, match="no namespace prefix"):
        split_prefix("listthings")


def test_register_and_get_mode():
    register_tools("zabbix", [
        {"name": "list_active_problems", "mode": "read"},
        {"name": "create_maintenance", "mode": "write"},
    ])
    assert get_tool_mode("zabbix", "list_active_problems") == "read"
    assert get_tool_mode("zabbix", "create_maintenance") == "write"


def test_get_tool_mode_unknown_returns_none():
    """fail-closed：未注册的工具 mode 返回 None，不再退化默认 'read'。

    历史 bug：默认 read 使 mode 元数据缺失时未知工具被当 read 放行，
    只读 token 可越权访问未知 write 工具。
    """
    assert get_tool_mode("ghost", "any_tool") is None
    register_tools("zabbix", [{"name": "list_active_problems", "mode": "read"}])
    try:
        assert get_tool_mode("zabbix", "undeclared_tool") is None
    finally:
        clear_tools("zabbix")


def test_resolve_target_known():
    register_tools("zabbix", [{"name": "list_active_problems", "mode": "read"}])
    try:
        server, tool, mode = resolve_target("zabbix_list_active_problems")
        assert (server, tool, mode) == ("zabbix", "list_active_problems", "read")
    finally:
        clear_tools("zabbix")


def test_resolve_target_unknown_tool_mode_none():
    """server 已注册但工具不在 registry → mode 为 None（不抛错、不默认 read）。"""
    register_tools("zabbix", [{"name": "list_active_problems", "mode": "read"}])
    try:
        server, tool, mode = resolve_target("zabbix_undeclared_tool")
        assert (server, tool) == ("zabbix", "undeclared_tool")
        assert mode is None
    finally:
        clear_tools("zabbix")


def test_resolve_target_unknown_server():
    with pytest.raises(UnknownServerError):
        resolve_target("ghost_tool")
