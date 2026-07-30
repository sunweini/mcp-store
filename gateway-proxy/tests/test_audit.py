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
