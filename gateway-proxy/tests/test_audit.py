"""Tests for audit.record_call_stream: the single XADD entry point for audit.

The proxy no longer touches MySQL (D1/D3): every call — success or failure —
is appended to the audit:calls stream as one XADD. MySQL persistence happens
in the gateway-admin consumer (XREADGROUP), which is out of scope here.
"""
import observability

from audit import record_call_stream, ERROR_TYPES, classify_error, build_journey, build_audit_meta


async def test_record_call_stream_xadds_success(fake_redis):
    await record_call_stream(
        meta={"time": "2026-08-07 12:00:00.000", "server": "tavily-mcp", "tool": "tavily_search",
              "op": "read", "token_name": "test", "latency_ms": 5, "trace_id": "t1"},
        status="ok", error_type=None, message="", journey=[],
    )
    entries = await fake_redis.xrange("audit:calls", count=1)
    assert len(entries) == 1
    msg = entries[0][1]
    assert msg["server"] == "tavily-mcp"
    assert msg["status"] == "ok"
    assert msg["journey"] == "[]"
    assert msg["time"] == "2026-08-07 12:00:00.000"  # 格式锁死，无毫秒精度变化


def test_error_types_are_the_documented_enum():
    """error_type 是管理前端错误类型 chips 的受限枚举，语义不变。"""
    assert set(ERROR_TYPES) == {
        "upstream_timeout", "permission_denied", "invalid_token",
        "upstream_error", "connection_error",
    }


async def test_record_call_stream_xadd_failure_never_raises(monkeypatch):
    """XADD 失败（Redis 挂）不抛异常——审计绝不断请求路径（D4 审计可丢），
    失败计入 audit_dropped_total 指标（observability 模块运行时取值）。"""
    class BoomRedis:
        async def xadd(self, *a, **kw):
            raise ConnectionError("redis down")

    monkeypatch.setattr("audit.get_redis", lambda: BoomRedis())
    counter = {"n": 0}

    class FakeCounter:
        def add(self, n, attrs):
            counter["n"] += n

    monkeypatch.setattr(observability, "AUDIT_DROPPED_TOTAL", FakeCounter())
    await record_call_stream(
        meta={"time": "2026-08-07 12:00:00.000", "server": "s", "tool": "t", "op": "read",
              "token_name": "n", "latency_ms": 1, "trace_id": "t"},
        status="ok",
    )  # 不抛
    assert counter["n"] == 1


# ─── classify_error ────────────────────────────────────────────────
# 异常→审计错误类型映射，属审计概念（已并入 audit.py，ADR-0001）

def test_classify_error_timeout():
    assert classify_error(__import__("httpx").TimeoutException("x")) == "upstream_timeout"


def test_classify_error_connect():
    assert classify_error(__import__("httpx").ConnectError("refused")) == "connection_error"


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


# ─── build_audit_meta: server/tool/op 由调用方注入 ─────────────────
# ADR-0001 后签名变为 (token_info, server, tool, op, latency_ms, trace_id)。
# server/tool/op 由 on_call_tool 的 AuthResult 提供——授权已解析出目标，
# 审计不再解析，故未注册 server 也不影响 server/tool 落空。

def test_build_audit_meta_injected_fields():
    meta = build_audit_meta(
        token_info={"name": "tok"},
        server="ghost-mcp",
        tool="web_search",
        op="read",
        latency_ms=1,
        trace_id="trace-ghost",
    )
    assert meta["server"] == "ghost-mcp"
    assert meta["tool"] == "web_search"
    assert meta["op"] == "read"
    assert meta["token_name"] == "tok"
    assert meta["latency_ms"] == 1
    assert meta["trace_id"] == "trace-ghost"
    # time 格式锁死 %Y-%m-%d %H:%M:%S.000（固定 .000，admin 消费者按此解析）
    assert meta["time"].endswith(".000")


def test_build_audit_meta_anonymous_token():
    """token_info=None -> token_name 为 "(anonymous)"。"""
    meta = build_audit_meta(None, "zabbix", "create_maintenance", "write", 1, "t2")
    assert meta["token_name"] == "(anonymous)"
    assert meta["op"] == "write"
