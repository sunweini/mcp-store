import json
from audit import record_failure, ERROR_TYPES


async def test_record_failure_writes_to_stream(fake_redis):
    journey = [
        {"stage": "client", "state": "ok", "ms": 2},
        {"stage": "auth", "state": "fail", "ms": 2},
        {"stage": "route", "state": "skip", "ms": 0},
    ]
    await record_failure(
        journey=journey,
        error_type="invalid_token",
        meta={
            "trace_id": "abc123",
            "server": "github",
            "tool": "list_repos",
            "op": "read",
            "message": "Token 无效",
            "latency_ms": 2,
            "time": "2026-07-30T12:41:55Z",
        },
    )
    entries = await fake_redis.xrange("audit:failures")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["error_type"] == "invalid_token"
    assert fields["trace"] == "abc123"
    parsed = json.loads(fields["journey"])
    assert parsed[1]["state"] == "fail"


def test_error_types_are_the_documented_enum():
    assert set(ERROR_TYPES) == {
        "upstream_timeout", "permission_denied", "invalid_token",
        "upstream_error", "connection_error",
    }


# ─── record_call (MySQL) ─────────────────────────────────────────

async def test_record_call_inserts_row(monkeypatch):
    """record_call 向 calls 表插一行，字段正确。"""
    import audit
    inserted = []

    class FakeCursor:
        async def execute(self, sql, args):
            inserted.append((sql, args))
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class FakeConn:
        def cursor(self): return FakeCursor()
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

    class FakePool:
        def acquire(self):
            class Cm:
                async def __aenter__(self): return FakeConn()
                async def __aexit__(self, *a): pass
            return Cm()

    monkeypatch.setattr(audit, "get_pool", lambda: _coro(FakePool()))
    await audit.record_call(
        meta={"trace_id": "t1", "server": "tavily-mcp", "tool": "tavily_search",
              "op": "read", "token_name": "tok", "latency_ms": 42,
              "time": "2026-08-04 10:00:00.000"},
        status="ok",
    )
    assert len(inserted) == 1
    sql, args = inserted[0]
    assert "INSERT INTO calls" in sql
    assert args[1] == "tavily-mcp"  # server
    assert args[6] == "ok"          # status


async def test_record_call_db_failure_does_not_raise(monkeypatch):
    """MySQL 异常不抛出（旁路审计不阻断主请求）。"""
    import audit
    async def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(audit, "get_pool", boom)
    await audit.record_call(
        meta={"trace_id": "t", "server": "s", "tool": "t", "op": "read",
              "token_name": "n", "latency_ms": 1, "time": "2026-08-04 10:00:00.000"},
        status="ok",
    )  # 不抛


# 辅助：把对象包成 awaitable
async def _coro(obj):
    return obj
