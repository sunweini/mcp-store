"""Tests for record_call_failure: journey building (ok/fail/skip states) +
token_name propagation.

The journey is a list of stages with state ok|fail|skip. The fail_stage
parameter determines where the journey breaks; stages after that are 'skip'.
This test exercises all three states and the silent-fail edge case where
fail_stage doesn't match any stage (journey is all 'ok' with no 'fail').
"""
import json
import pytest

from middleware import record_call_failure
from routing import register_tools, clear_tools


@pytest.fixture(autouse=True)
def register_zabbix_tools():
    register_tools("zabbix", [
        {"name": "list_active_problems", "mode": "read"},
    ])
    yield
    clear_tools("zabbix")


async def test_record_call_failure_auth_stage(fake_redis):
    """fail_stage='auth' -> journey breaks at auth, route+backend are skip."""
    await record_call_failure(
        token_info={"name": "my-token"},
        mcp_name="zabbix_list_active_problems",
        error_type="invalid_token",
        message="bad token",
        latency_ms=3,
        trace_id="trace-001",
        fail_stage="auth",
    )
    entries = await fake_redis.xrange("audit:failures")
    assert len(entries) == 1
    _, fields = entries[0]
    journey = json.loads(fields["journey"])

    # stages = ["client", "gateway", "auth", "route", "zabbix"]
    assert len(journey) == 5
    assert journey[0] == {"stage": "client", "state": "ok", "ms": 0}
    assert journey[1] == {"stage": "gateway", "state": "ok", "ms": 0}
    assert journey[2] == {"stage": "auth", "state": "fail", "ms": 3}
    assert journey[3] == {"stage": "route", "state": "skip", "ms": 0}
    assert journey[4] == {"stage": "zabbix", "state": "skip", "ms": 0}

    # token_name must be propagated to the audit record.
    assert fields["token_name"] == "my-token"
    assert fields["trace"] == "trace-001"
    assert fields["error_type"] == "invalid_token"


async def test_record_call_failure_route_stage(fake_redis):
    """fail_stage='route' -> auth is ok, route fails, backend is skip."""
    await record_call_failure(
        token_info={"name": "tok"},
        mcp_name="zabbix_list_active_problems",
        error_type="permission_denied",
        message="no access",
        latency_ms=1,
        trace_id="trace-002",
        fail_stage="route",
    )
    entries = await fake_redis.xrange("audit:failures")
    _, fields = entries[0]
    journey = json.loads(fields["journey"])

    assert journey[2] == {"stage": "auth", "state": "ok", "ms": 0}
    assert journey[3] == {"stage": "route", "state": "fail", "ms": 1}
    assert journey[4] == {"stage": "zabbix", "state": "skip", "ms": 0}


async def test_record_call_failure_backend_stage(fake_redis):
    """fail_stage=server_name -> everything before is ok, server is fail."""
    await record_call_failure(
        token_info={"name": "tok"},
        mcp_name="zabbix_list_active_problems",
        error_type="upstream_timeout",
        message="timed out",
        latency_ms=5000,
        trace_id="trace-003",
        fail_stage="zabbix",
    )
    entries = await fake_redis.xrange("audit:failures")
    _, fields = entries[0]
    journey = json.loads(fields["journey"])

    assert journey[0]["state"] == "ok"
    assert journey[1]["state"] == "ok"
    assert journey[2]["state"] == "ok"
    assert journey[3]["state"] == "ok"
    assert journey[4] == {"stage": "zabbix", "state": "fail", "ms": 5000}


async def test_record_call_failure_anonymous_token(fake_redis):
    """token_info=None -> token_name is '(anonymous)'."""
    await record_call_failure(
        token_info=None,
        mcp_name="zabbix_list_active_problems",
        error_type="invalid_token",
        message="no token",
        latency_ms=0,
        trace_id="trace-004",
        fail_stage="auth",
    )
    entries = await fake_redis.xrange("audit:failures")
    _, fields = entries[0]
    assert fields["token_name"] == "(anonymous)"


async def test_record_call_failure_unresolvable_name(fake_redis):
    """When mcp_name can't be split (no underscore), journey uses 'backend' stage."""
    # 无下划线 -> split_prefix 抛 ValueError，server/tool 保持空、op 默认 read。
    # 注意名字本身不能含下划线：'no_namespace_here' 会被 split_prefix 切出
    # server='no'（新语义），那样就到不了 ValueError 分支。
    await record_call_failure(
        token_info={"name": "tok"},
        mcp_name="rootlevel",
        error_type="permission_denied",
        message="unknown",
        latency_ms=2,
        trace_id="trace-005",
        fail_stage="route",
    )
    entries = await fake_redis.xrange("audit:failures")
    _, fields = entries[0]
    journey = json.loads(fields["journey"])

    # server is "" so stages list uses "backend" as the last stage name.
    assert journey[4]["stage"] == "backend"
    assert journey[3] == {"stage": "route", "state": "fail", "ms": 2}
    # server/tool fields are empty strings (split_prefix failed).
    assert fields["server"] == ""
    assert fields["tool"] == ""
    assert fields["op"] == "read"


async def test_record_call_failure_ghost_server_resolved(fake_redis):
    """server 未注册（禁用后 registry 卸载）时，server/tool 仍解析出来。

    生产 bug：server 禁用后 resolve_target 抛 UnknownServerError，审计字段
    落空。修复后 server/tool 由 split_prefix（纯前缀切分，不查 registry）
    解析；op 在 registry 缺失时默认 read。
    """
    # 不注册 ghost-mcp：模拟禁用后从 TOOL_REGISTRY 卸载的状态
    await record_call_failure(
        token_info={"name": "tok"},
        mcp_name="ghost-mcp_web_search",
        error_type="permission_denied",
        message="Denied: ghost-mcp_web_search",
        latency_ms=2,
        trace_id="trace-ghost",
        fail_stage="route",
    )
    entries = await fake_redis.xrange("audit:failures")
    _, fields = entries[0]
    journey = json.loads(fields["journey"])

    assert fields["server"] == "ghost-mcp"
    assert fields["tool"] == "web_search"
    assert fields["op"] == "read"  # resolve_target 失败 -> 默认 read
    # journey 末段用真实 server 名而非回退 'backend'
    assert journey[4]["stage"] == "ghost-mcp"
