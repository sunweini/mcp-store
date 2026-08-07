"""Tests for the audit failure path: record_call_stream status=fail.

单流 audit:calls 承载成功+失败全量；失败路径把 message（错误文案）与
journey（请求轨迹，ok/fail/skip 三段状态）完整写入 stream 条目，
供消费者落 MySQL 后失败面板直接展示。
"""
from audit import record_call_stream


async def test_record_call_stream_fail_path(fake_redis):
    await record_call_stream(
        meta={"time": "2026-08-07 12:00:00.000", "server": "zabbix-mcp", "tool": "zabbix_list",
              "op": "read", "token_name": "t", "latency_ms": 30, "trace_id": "t2"},
        status="fail", error_type="upstream_timeout", message="timeout",
        journey=[{"stage": "auth", "state": "fail", "ms": 30}],
    )
    entries = await fake_redis.xrange("audit:calls", count=1)
    msg = entries[0][1]
    assert msg["status"] == "fail"
    assert msg["error_type"] == "upstream_timeout"
    assert msg["message"] == "timeout"
    assert '"stage": "auth"' in msg["journey"]


async def test_record_call_stream_fail_without_message_journey(fake_redis):
    """不传 message/journey -> 落 '' 和 '[]'（消费者 NOT NULL 列不能写 None）。"""
    await record_call_stream(
        meta={"time": "2026-08-07 12:00:00.000", "server": "zabbix-mcp", "tool": "zabbix_list",
              "op": "read", "token_name": "t", "latency_ms": 5, "trace_id": "t3"},
        status="fail", error_type="permission_denied",
    )
    entries = await fake_redis.xrange("audit:calls", count=1)
    msg = entries[0][1]
    assert msg["message"] == ""
    assert msg["journey"] == "[]"
