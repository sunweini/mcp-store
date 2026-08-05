"""Tests for middleware: permission check + error classification + audit wiring."""
import pytest
import httpx

from middleware import build_journey, check_call_permission, classify_error
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


# ─── build_journey ────────────────────────────────────────────────
# 失败面板数据源统一到 MySQL 后，on_call_tool 与 record_call_failure 共用
# 此函数构建轨迹；state 推演逻辑（ok/fail/skip）必须与旧内联实现完全一致

def test_build_journey_fail_in_middle():
    """fail_stage 之前的 stage 是 ok，fail stage 带总耗时，之后是 skip。"""
    journey = build_journey("auth", "zabbix", 3)
    assert journey == [
        {"stage": "client", "state": "ok", "ms": 0},
        {"stage": "gateway", "state": "ok", "ms": 0},
        {"stage": "auth", "state": "fail", "ms": 3},
        {"stage": "route", "state": "skip", "ms": 0},
        {"stage": "zabbix", "state": "skip", "ms": 0},
    ]


def test_build_journey_fail_at_last_stage():
    """后端 server 阶段失败：前面全 ok，无 skip。"""
    journey = build_journey("zabbix", "zabbix", 5000)
    assert [s["state"] for s in journey] == ["ok", "ok", "ok", "ok", "fail"]
    assert journey[4] == {"stage": "zabbix", "state": "fail", "ms": 5000}


def test_build_journey_empty_server_falls_back_to_backend():
    """server 解析不出（空串）时末段 stage 名回退为 'backend'。"""
    journey = build_journey("route", "", 2)
    assert journey[4]["stage"] == "backend"
    assert journey[3] == {"stage": "route", "state": "fail", "ms": 2}


def test_build_journey_unmatched_stage_all_ok():
    """fail_stage 不匹配任何 stage -> 全 ok（与旧内联实现的边界行为一致）。"""
    journey = build_journey("nowhere", "zabbix", 1)
    assert all(s["state"] == "ok" for s in journey)
