"""Tests for middleware: permission check + error classification + audit wiring."""
import pytest
import httpx

from middleware import (
    build_journey,
    build_audit_meta,
    check_call_permission,
    classify_error,
)
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
# on_call_tool 的拒绝/异常两条失败路径共用此函数构建轨迹；
# state 推演逻辑（ok/fail/skip）是消费者落 MySQL 后失败面板轨迹的数据源

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


# ─── build_audit_meta: 未注册 server 仍能解析 server/tool ─────────
# 生产 bug：server 禁用后从 TOOL_REGISTRY 卸载，resolve_target 抛
# UnknownServerError，审计记录 server/tool 落空。修复后由 split_prefix
# （纯前缀切分，不查 registry）解析，op 在 registry 缺失时默认 read。
# build_audit_meta 是 on_call_tool 三条审计路径共用 meta 的唯一出口，
# 此行为必须保持（record_call_stream 收到的字段依赖它）。

def test_build_audit_meta_ghost_server_resolved():
    """server 未注册（禁用后 registry 卸载）时，meta 仍带 server/tool。"""
    meta = build_audit_meta(
        token_info={"name": "tok"},
        mcp_name="ghost-mcp_web_search",
        latency_ms=1,
        trace_id="trace-ghost",
    )
    assert meta["server"] == "ghost-mcp"
    assert meta["tool"] == "web_search"
    assert meta["op"] == "read"  # resolve_target 失败 -> 默认 read
    assert meta["token_name"] == "tok"
    assert meta["latency_ms"] == 1
    assert meta["trace_id"] == "trace-ghost"
    # time 格式锁死 %Y-%m-%d %H:%M:%S.000（固定 .000，admin 消费者按此解析）
    assert meta["time"] == "2026-08-07 00:00:00.000" or meta["time"].endswith(".000")


def test_build_audit_meta_op_registry_lookup():
    """server 未注册时 op 降级 read；正常注册路径 op 仍取 registry 的 mode。"""
    # ghost 未注册 -> op 默认 read
    meta = build_audit_meta(None, "ghost-mcp_web_search", 1, "t1")
    assert meta["op"] == "read"
    # token_info=None -> token_name 为 "(anonymous)"
    assert meta["token_name"] == "(anonymous)"

    # zabbix 已注册 write 工具 -> op 取真实 mode（fixture 已注册）
    meta = build_audit_meta(None, "zabbix_create_maintenance", 1, "t2")
    assert meta["server"] == "zabbix"
    assert meta["tool"] == "create_maintenance"
    assert meta["op"] == "write"
