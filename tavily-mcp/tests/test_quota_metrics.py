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
        "key": key, "provider": "tavily", "enabled": True,
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
    pool = KeyPool("tavily", FakeRedis(records), AsyncMock(), quota_default=1000)
    await pool.reload()
    return pool


async def test_health_snapshot_aggregates_pool(monkeypatch):
    """health_snapshot 取全池最低档：ratio/remaining 取 min，池大小与
    invalid 计数正确，unknown-remaining 不拉低最低值。"""
    calls = []
    monkeypatch.setattr("key_pool.record_quota_metrics",
                        lambda provider, snap: calls.append(snap))
    records = {
        "k1": _rec("k1", "tvly-a", remaining=50),     # ratio 0.05 → critical
        "k2": _rec("k2", "tvly-b", remaining=80),     # ratio 0.08 → warning
        "k3": _rec("k3", "tvly-c", remaining=None),   # unknown → 不参与
        "k4": _rec("k4", "tvly-d", status="invalid"),  # invalid，无 remaining
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
        "k1": _rec("k1", "tvly-a", remaining=900),
        "k2": _rec("k2", "tvly-b", remaining=800),
    }
    pool = await _make_pool(records)
    assert calls[-1][0] == "tavily"
    assert calls[-1][1]["pool_size"] == 2


async def test_on_error_invalid_emits_metrics(monkeypatch):
    """key 被永久剔除后指标立即刷新（不用等下次 reload）。"""
    calls = []
    monkeypatch.setattr("key_pool.record_quota_metrics",
                        lambda provider, snap: calls.append(snap))
    records = {
        "k1": _rec("k1", "tvly-a", remaining=900),
        "k2": _rec("k2", "tvly-b", remaining=800),
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
    records = {"k1": _rec("k1", "tvly-a", remaining=900)}
    pool = await _make_pool(records)
    calls.clear()
    await pool.on_error("k1", ErrorKind.RATE_LIMIT)
    assert calls == []


def test_quota_level_mapping():
    """档位映射：<10% warning，<5% critical，0/None-with-0 exhausted，
    >=10% 全档位归零。用假 instrument 验证 label 取值与档位写 1/0。"""
    from telemetry import (QUOTA_LEVEL_CRITICAL, QUOTA_LEVEL_EXHAUSTED,
                           QUOTA_LEVEL_WARNING, record_quota_metrics)

    class FakeGauge:
        def __init__(self):
            self.sets = []

        def set(self, value, attributes=None):
            self.sets.append((value, attributes))

    class FakeInstruments:
        remaining = FakeGauge()
        ratio = FakeGauge()
        size = FakeGauge()
        invalid = FakeGauge()

    fakes = FakeInstruments()
    monkey = pytest.MonkeyPatch()
    monkey.setattr("telemetry.SEARCH_QUOTA_REMAINING", fakes.remaining)
    monkey.setattr("telemetry.SEARCH_QUOTA_RATIO", fakes.ratio)
    monkey.setattr("telemetry.SEARCH_KEY_POOL_SIZE", fakes.size)
    monkey.setattr("telemetry.SEARCH_KEY_INVALID_TOTAL", fakes.invalid)

    def _ratio_labels():
        return {a[1]["level"]: a[0] for a in fakes.ratio.sets}

    try:
        # warning 档位：warning=1，其余 0
        record_quota_metrics("tavily", {"lowest_ratio": 0.08, "lowest_remaining": 80,
                                        "pool_size": 2, "invalid_count": 1})
        labels = _ratio_labels()
        assert labels == {QUOTA_LEVEL_WARNING: 1, QUOTA_LEVEL_CRITICAL: 0,
                          QUOTA_LEVEL_EXHAUSTED: 0}
        # critical 档位
        record_quota_metrics("tavily", {"lowest_ratio": 0.03, "lowest_remaining": 30,
                                        "pool_size": 2, "invalid_count": 0})
        labels = _ratio_labels()
        assert labels == {QUOTA_LEVEL_WARNING: 0, QUOTA_LEVEL_CRITICAL: 1,
                          QUOTA_LEVEL_EXHAUSTED: 0}
        # exhausted 档位
        record_quota_metrics("tavily", {"lowest_ratio": 0.0, "lowest_remaining": 0,
                                        "pool_size": 2, "invalid_count": 0})
        labels = _ratio_labels()
        assert labels == {QUOTA_LEVEL_WARNING: 0, QUOTA_LEVEL_CRITICAL: 0,
                          QUOTA_LEVEL_EXHAUSTED: 1}
        # remaining 未知（None）但 ratio None → 全档位归零（不编造档位）
        record_quota_metrics("tavily", {"lowest_ratio": None, "lowest_remaining": None,
                                        "pool_size": 2, "invalid_count": 0})
        labels = _ratio_labels()
        assert labels == {QUOTA_LEVEL_WARNING: 0, QUOTA_LEVEL_CRITICAL: 0,
                          QUOTA_LEVEL_EXHAUSTED: 0}
        # ratio None 但 remaining==0 → 兜底 exhausted
        record_quota_metrics("tavily", {"lowest_ratio": None, "lowest_remaining": 0,
                                        "pool_size": 2, "invalid_count": 0})
        labels = _ratio_labels()
        assert labels == {QUOTA_LEVEL_WARNING: 0, QUOTA_LEVEL_CRITICAL: 0,
                          QUOTA_LEVEL_EXHAUSTED: 1}
        # remaining/ratio 正常（>=10%）→ 档位全归零（告警恢复语义）
        record_quota_metrics("tavily", {"lowest_ratio": 0.5, "lowest_remaining": 500,
                                        "pool_size": 2, "invalid_count": 0})
        labels = _ratio_labels()
        assert labels == {QUOTA_LEVEL_WARNING: 0, QUOTA_LEVEL_CRITICAL: 0,
                          QUOTA_LEVEL_EXHAUSTED: 0}
        # size/invalid 每轮都发（第 4 轮 remaining=None 不发，
        # 故 remaining 只 5 次——unknown 不发是正确行为）
        assert len(fakes.size.sets) == 6
        assert len(fakes.invalid.sets) == 6
        assert len(fakes.remaining.sets) == 5
        # gauge set 写的是当前值（绝对），非累计增量
        assert fakes.remaining.sets[-1] == (500, {"provider": "tavily"})
        assert fakes.size.sets[-1] == (2, {"provider": "tavily"})
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
        record_quota_metrics("tavily", {"lowest_ratio": 0.01, "lowest_remaining": 5,
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


def test_gauge_export_semantics():
    """导出语义验证（真实 PrometheusMetricReader + registry scrape）：
    gauge.set 是当前值语义——reload 两次后 remaining 不翻倍；档位
    恢复后归零、可再次触发。up_down_counter 累计语义（re-review 实证
    add(800);add(700)→1500）下这两条均失败，此测试守卫回归。

    注：PrometheusMetricReader 在同一时刻的第二次 scrape 返回空
    （SDK 周期采集语义），故每个场景编排为「set 序列 → force_flush →
    一次 scrape 断言」。
    """
    from prometheus_client import CollectorRegistry, generate_latest
    from opentelemetry import metrics
    from opentelemetry.exporter.prometheus import PrometheusMetricReader
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource

    from telemetry import record_quota_metrics

    registry = CollectorRegistry()
    reader = PrometheusMetricReader(registry=registry)
    provider = MeterProvider(resource=Resource.create({"service.name": "t"}),
                             metric_readers=[reader])
    metrics.set_meter_provider(provider)
    meter = metrics.get_meter("probe")

    remaining = meter.create_gauge("test_remaining", unit="1", description="d")
    ratio = meter.create_gauge("test_ratio", unit="1", description="d")
    size = meter.create_gauge("test_size", unit="1", description="d")
    invalid = meter.create_gauge("test_invalid", unit="1", description="d")

    def scrape():
        """一次完整 scrape；解析所有 sample，返回 {name: {label_tuple: value}}。

        PrometheusMetricReader 在同一时刻的第二次 scrape 返回空（SDK
        周期采集语义），故每个场景只调一次 scrape、断言多个指标。
        """
        out = {}
        for line in generate_latest(registry).decode().splitlines():
            if not line.startswith("test_") or "{" not in line:
                continue
            name, rest = line.split("{", 1)
            sample_labels, _, value = rest.rpartition("}")
            labels = {}
            for part in sample_labels[:-1].split(","):
                k, _, v = part.partition("=")
                labels[k] = v.strip('"')
            labels.pop("otel_scope_name", None)
            labels.pop("otel_scope_schema_url", None)
            labels.pop("otel_scope_version", None)
            out.setdefault(name, {})[tuple(sorted(labels.items()))] = float(value)
        return out

    def snap(ratio_v, remaining_v, pool, invalid_v=0):
        return {"lowest_ratio": ratio_v, "lowest_remaining": remaining_v,
                "pool_size": pool, "invalid_count": invalid_v}

    monkey = pytest.MonkeyPatch()
    monkey.setattr("telemetry.SEARCH_QUOTA_REMAINING", remaining)
    monkey.setattr("telemetry.SEARCH_QUOTA_RATIO", ratio)
    monkey.setattr("telemetry.SEARCH_KEY_POOL_SIZE", size)
    monkey.setattr("telemetry.SEARCH_KEY_INVALID_TOTAL", invalid)
    try:
        # 两次 reload：remaining 900 → 700，pool 3 key；set 语义下导出
        # 最新值而非累计和（累计语义会导出 1600/7——re-review 实测缺陷）
        record_quota_metrics("t", snap(0.9, 900, 3))
        record_quota_metrics("t", snap(0.7, 700, 3))
        reader.force_flush()
        s = scrape()
        assert s["test_remaining"][(("provider", "t"),)] == 700
        assert s["test_size"][(("provider", "t"),)] == 3

        # 档位触发后恢复：warning 先 1 后 0（累计语义下残留累计值，无法归零）
        record_quota_metrics("t", snap(0.08, 80, 3))
        record_quota_metrics("t", snap(0.5, 500, 3))
        reader.force_flush()
        s = scrape()
        assert s["test_ratio"][(("level", "warning"), ("provider", "t"))] == 0

        # 恢复后可再次触发（告警可重复发生）
        record_quota_metrics("t", snap(0.02, 20, 3))
        reader.force_flush()
        s = scrape()
        assert s["test_ratio"][(("level", "critical"), ("provider", "t"))] == 1

        # invalid 传绝对值不超报：两次 reload 后 invalid=1（counter 传
        # 绝对值会累计到 2）
        record_quota_metrics("t", snap(0.9, 900, 3, invalid_v=1))
        record_quota_metrics("t", snap(0.9, 900, 3, invalid_v=1))
        reader.force_flush()
        s = scrape()
        assert s["test_invalid"][(("provider", "t"),)] == 1
    finally:
        monkey.undo()
