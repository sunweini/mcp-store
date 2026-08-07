"""Tests for audit.record_call_stream: the single XADD entry point for audit.

The proxy no longer touches MySQL (D1/D3): every call — success or failure —
is appended to the audit:calls stream as one XADD. MySQL persistence happens
in the gateway-admin consumer (XREADGROUP), which is out of scope here.
"""
import observability

from audit import record_call_stream, ERROR_TYPES


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
