"""Quota-metric wiring tests (final-review I-1).

验证两条接线：
1. KeyPool.reload() / on_error(INVALID) 后 quota 指标被刷新
   （record_quota_metrics 被调用，数据来自 health_snapshot）
2. record_quota_metrics 的档位聚合规则（warning<10% / critical<5% /
   exhausted=0）与「metrics 全 None 时 no-op」的 guard

测试用 monkeypatch 替换 telemetry.record_quota_metrics 捕获调用，
不依赖真实 OTel/Prometheus（metrics 在测试进程未 init，保持 None）。
"""
import json
from unittest.mock import AsyncMock

import pytest

from key_pool import ErrorKind, KeyPool


def _rec(key_id, key, **over):
    base = {
        "key": key, "provider": "serpapi", "enabled": True,
        "monthly_quota": 1000, "status": "active",
        "cooldown_until": None, "remaining": None, "last_error": None,
    }
    base.update(over)
    return json.dumps(base)


class FakeRedis:
    """Minimal async Redis fake (same shape as test_key_pool.FakeRedis)."""

    def __init__(self, records):
        self._records = dict(records)
        self.hset_calls = []

    def _fields_of(self, name):
        owned = self._records.get(name)
        if isinstance(owned, dict):
            merged = dict(self._records)
            merged.pop(name, None)
            merged.update(owned)
            return merged
        return self._records

    async def hgetall(self, name):
        return dict(self._fields_of(name))

    async def hset(self, name, key=None, value=None, mapping=None):
        if mapping is None:
            mapping = {key: value}
        existing = self._records.get(name)
        if not isinstance(existing, dict):
            existing = {}
            self._records[name] = existing
        for field, val in mapping.items():
            existing[field] = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        self.hset_calls.append((name, mapping))
        return 1

    async def zadd(self, name, mapping):
        return 1

    async def expire(self, name, seconds):
        return True


async def _make_pool(records):
    pool = KeyPool("serpapi", FakeRedis(records), AsyncMock(), quota_default=1000)
    await pool.reload()
    return pool


async def test_health_snapshot_aggregates_pool(monkeypatch):
    """health_snapshot 取全池最低档：ratio/remaining 取 min，池大小与
    invalid 计数正确，unknown-remaining 不拉低最低值。"""
    calls = []
    monkeypatch.setattr("key_pool.record_quota_metrics",
                        lambda provider, snap: calls.append(snap))
    records = {
        "k1": _rec("k1", "srp-a", remaining=50),     # ratio 0.05 → critical
        "k2": _rec("k2", "srp-b", remaining=80),     # ratio 0.08 → warning
        "k3": _rec("k3", "srp-c", remaining=None),   # unknown → 不参与
        "k4": _rec("k4", "srp-d", status="invalid"),  # invalid，无 remaining
    }
    await _make_pool(records)
    snap = calls[-1]
    assert snap["lowest_ratio"] == 0.05
    assert snap["lowest_remaining"] == 50
    assert snap["pool_size"] == 4
    assert snap["invalid_count"] == 1


async def test_reload_emits_metrics(monkeypatch):
    """reload（含启动）后指标刷新——热更新改配额/增删 key 立即反映。"""
    calls = []
    monkeypatch.setattr("key_pool.record_quota_metrics",
                        lambda provider, snap: calls.append((provider, snap)))
    records = {
        "k1": _rec("k1", "srp-a", remaining=900),
        "k2": _rec("k2", "srp-b", remaining=800),
    }
    pool = await _make_pool(records)
    assert calls[-1][0] == "serpapi"
    assert calls[-1][1]["pool_size"] == 2


async def test_on_error_invalid_emits_metrics(monkeypatch):
    """key 被永久剔除后指标立即刷新（不用等下次 reload）。"""
    calls = []
    monkeypatch.setattr("key_pool.record_quota_metrics",
                        lambda provider, snap: calls.append(snap))
    records = {
        "k1": _rec("k1", "srp-a", remaining=900),
        "k2": _rec("k2", "srp-b", remaining=800),
    }
    pool = await _make_pool(records)
    calls.clear()
    await pool.on_error("k1", ErrorKind.INVALID)
    assert len(calls) == 1
    assert calls[-1]["invalid_count"] == 1


async def test_rate_limit_does_not_emit_metrics(monkeypatch):
    """临时冷却不改池长期健康——RATE_LIMIT 不刷新指标（避免无谓告警抖动）。"""
    calls = []
    monkeypatch.setattr("key_pool.record_quota_metrics",
                        lambda provider, snap: calls.append(snap))
    records = {"k1": _rec("k1", "srp-a", remaining=900)}
    pool = await _make_pool(records)
    calls.clear()
    await pool.on_error("k1", ErrorKind.RATE_LIMIT)
    assert calls == []


def test_quota_level_mapping():
    """档位映射：<10% warning，<5% critical，0/None-with-0 exhausted，
    >=10% 不发 ratio（正常不告警）。用假 instrument 验证 label 取值。"""
    from telemetry import (QUOTA_LEVEL_CRITICAL, QUOTA_LEVEL_EXHAUSTED,
                           QUOTA_LEVEL_WARNING, record_quota_metrics)

    class FakeCounter:
        def __init__(self):
            self.adds = []

        def add(self, value, attributes=None):
            self.adds.append((value, attributes))

    class FakeInstruments:
        remaining = FakeCounter()
        ratio = FakeCounter()
        size = FakeCounter()
        invalid = FakeCounter()

    fakes = FakeInstruments()
    monkey = pytest.MonkeyPatch()
    monkey.setattr("telemetry.SEARCH_QUOTA_REMAINING", fakes.remaining)
    monkey.setattr("telemetry.SEARCH_QUOTA_RATIO", fakes.ratio)
    monkey.setattr("telemetry.SEARCH_KEY_POOL_SIZE", fakes.size)
    monkey.setattr("telemetry.SEARCH_KEY_INVALID_TOTAL", fakes.invalid)
    try:
        record_quota_metrics("serpapi", {"lowest_ratio": 0.08, "lowest_remaining": 80,
                                        "pool_size": 2, "invalid_count": 1})
        assert fakes.ratio.adds[-1][1] == {"provider": "serpapi", "level": QUOTA_LEVEL_WARNING}
        record_quota_metrics("serpapi", {"lowest_ratio": 0.03, "lowest_remaining": 30,
                                        "pool_size": 2, "invalid_count": 0})
        assert fakes.ratio.adds[-1][1] == {"provider": "serpapi", "level": QUOTA_LEVEL_CRITICAL}
        record_quota_metrics("serpapi", {"lowest_ratio": 0.0, "lowest_remaining": 0,
                                        "pool_size": 2, "invalid_count": 0})
        assert fakes.ratio.adds[-1][1] == {"provider": "serpapi", "level": QUOTA_LEVEL_EXHAUSTED}
        # remaining 未知（None）但 ratio None → 不发 ratio（不编造档位）
        record_quota_metrics("serpapi", {"lowest_ratio": None, "lowest_remaining": None,
                                        "pool_size": 2, "invalid_count": 0})
        # ratio None 但 remaining==0 → 兜底 exhausted
        record_quota_metrics("serpapi", {"lowest_ratio": None, "lowest_remaining": 0,
                                        "pool_size": 2, "invalid_count": 0})
        assert fakes.ratio.adds[-1][1] == {"provider": "serpapi", "level": QUOTA_LEVEL_EXHAUSTED}
        # remaining/ratio 正常（>=10%）→ 只发 size/invalid/remaining，不发 ratio
        record_quota_metrics("serpapi", {"lowest_ratio": 0.5, "lowest_remaining": 500,
                                        "pool_size": 2, "invalid_count": 0})
        last = fakes.ratio.adds[-1]
        assert last == (1, {"provider": "serpapi", "level": QUOTA_LEVEL_EXHAUSTED})
        # size/invalid/remaining 每轮都发（第 4 轮 remaining=None 不发，
        # 故 remaining 只 5 次——unknown 不发是正确行为）
        assert len(fakes.size.adds) == 6
        assert len(fakes.invalid.adds) == 6
        assert len(fakes.remaining.adds) == 5
    finally:
        monkey.undo()


def test_metrics_none_guard(monkeypatch):
    """telemetry 未初始化（全 None）时 record_quota_metrics no-op 不抛。"""
    from telemetry import record_quota_metrics

    monkey = pytest.MonkeyPatch()
    for name in ("SEARCH_QUOTA_REMAINING", "SEARCH_QUOTA_RATIO",
                 "SEARCH_KEY_POOL_SIZE", "SEARCH_KEY_INVALID_TOTAL"):
        monkey.setattr(f"telemetry.{name}", None)
    try:
        record_quota_metrics("serpapi", {"lowest_ratio": 0.01, "lowest_remaining": 5,
                                        "pool_size": 1, "invalid_count": 0})
    finally:
        monkey.undo()


def test_key_id_never_in_label():
    """接线不变式：quota 指标 label 只有 provider/level，无 key 维度。"""
    from telemetry import record_quota_metrics
    import inspect

    src = inspect.getsource(record_quota_metrics)
    assert '"key_id"' not in src
    assert '"key"' not in src
