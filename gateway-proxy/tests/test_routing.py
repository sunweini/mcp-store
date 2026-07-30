"""Tests for namespace prefix routing + tool mode registry."""
import pytest
from routing import split_prefix, register_tools, get_tool_mode, resolve_target, UnknownServerError


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


def test_resolve_target_known():
    register_tools("zabbix", [{"name": "list_active_problems", "mode": "read"}])
    server, tool, mode = resolve_target("zabbix_list_active_problems")
    assert (server, tool, mode) == ("zabbix", "list_active_problems", "read")


def test_resolve_target_unknown_server():
    with pytest.raises(UnknownServerError):
        resolve_target("ghost_tool")
