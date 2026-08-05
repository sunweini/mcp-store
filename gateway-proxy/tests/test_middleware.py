"""Tests for middleware: permission check + error classification + audit wiring."""
import pytest
import httpx

from middleware import (
    build_journey,
    check_call_permission,
    classify_error,
    record_call_audit,
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


# ─── record_call_audit: 未注册 server 仍能解析 server/tool ─────────
# 生产 bug：server 禁用后从 TOOL_REGISTRY 卸载，resolve_target 抛
# UnknownServerError，审计记录 server/tool 落空。修复后由 split_prefix
# （纯前缀切分，不查 registry）解析，op 在 registry 缺失时默认 read。

async def test_record_call_audit_ghost_server_resolved(monkeypatch):
    """server 未注册（禁用后 registry 卸载）时，calls 行仍带 server/tool。"""
    rows = []

    async def fake_record_call(meta, status, error_type=None, message=None, journey=None):
        rows.append({"meta": meta, "status": status, "error_type": error_type})

    monkeypatch.setattr("middleware.record_call", fake_record_call)
    # 不注册 ghost-mcp：模拟禁用后从 TOOL_REGISTRY 卸载的状态
    await record_call_audit(
        token_info={"name": "tok"},
        mcp_name="ghost-mcp_web_search",
        latency_ms=1,
        trace_id="trace-ghost",
        status="fail",
        error_type="permission_denied",
        message="Denied: ghost-mcp_web_search",
    )
    assert len(rows) == 1
    meta = rows[0]["meta"]
    assert meta["server"] == "ghost-mcp"
    assert meta["tool"] == "web_search"
    assert meta["op"] == "read"  # resolve_target 失败 -> 默认 read
    assert rows[0]["status"] == "fail"
    assert rows[0]["error_type"] == "permission_denied"


async def test_record_call_audit_ghost_server_op_registry_lookup(monkeypatch):
    """server 未注册时 op 降级 read；正常注册路径 op 仍取 registry 的 mode。"""
    rows = []

    async def fake_record_call(meta, status, error_type=None, message=None, journey=None):
        rows.append(meta)

    monkeypatch.setattr("middleware.record_call", fake_record_call)

    # ghost 未注册 -> op 默认 read
    await record_call_audit(
        token_info=None, mcp_name="ghost-mcp_web_search",
        latency_ms=1, trace_id="t1", status="fail",
        error_type="permission_denied", message="Denied",
    )
    assert rows[-1]["op"] == "read"

    # zabbix 已注册 write 工具 -> op 取真实 mode（fixture 已注册）
    await record_call_audit(
        token_info=None, mcp_name="zabbix_create_maintenance",
        latency_ms=1, trace_id="t2", status="ok",
    )
    assert rows[-1]["server"] == "zabbix"
    assert rows[-1]["tool"] == "create_maintenance"
    assert rows[-1]["op"] == "write"
