"""Tests for middleware: permission check + error classification + audit wiring."""
import pytest
import httpx

from middleware import check_call_permission, classify_error
from routing import register_tools, clear_tools


@pytest.fixture(autouse=True)
def register_zabbix_tools():
    """Register the zabbix server with read + write tools so resolve_target succeeds.

    The brief's tests call check_call_permission with zabbix_list_active_problems
    (read) and zabbix_create_maintenance (write); resolve_target needs the server
    registered in TOOL_REGISTRY or it raises UnknownServerError.
    """
    register_tools("zabbix", [
        {"name": "list_active_problems", "mode": "read"},
        {"name": "create_maintenance", "mode": "write"},
    ])
    yield
    clear_tools("zabbix")


def test_check_call_permission_allows_read():
    # token has zabbix read; calling a read tool -> allowed
    token_info = {"permissions": {"zabbix": {"read": True, "write": False}}}
    ok, err = check_call_permission(token_info, "zabbix_list_active_problems")
    assert ok is True
    assert err is None


def test_check_call_permission_denies_write():
    token_info = {"permissions": {"zabbix": {"read": True, "write": False}}}
    ok, err = check_call_permission(token_info, "zabbix_create_maintenance")
    assert ok is False
    assert err == "permission_denied"


def test_check_call_permission_denies_unknown_server():
    token_info = {"permissions": {"zabbix": {"read": True}}}
    ok, err = check_call_permission(token_info, "ghost_tool")
    assert ok is False
    assert err == "permission_denied"


def test_classify_error_timeout():
    assert classify_error(httpx.TimeoutException("x")) == "upstream_timeout"


def test_classify_error_connect():
    assert classify_error(httpx.ConnectError("refused")) == "connection_error"


def test_classify_error_generic():
    assert classify_error(ValueError("boom")) == "upstream_error"
