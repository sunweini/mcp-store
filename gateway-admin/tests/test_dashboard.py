"""Tests for Dashboard API: MySQL metrics + failures Stream."""
import json
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


# ── metrics.py unit tests (Prometheus text parser, not dashboard endpoints) ──
# metrics.py 保留不动，其单测继续验证解析逻辑

async def test_query_prometheus(monkeypatch):
    from metrics import query_prometheus, fetch_metrics
    async def fake_fetch():
        return {"gateway_requests_total": [({"status": "ok"}, 42.0)]}
    monkeypatch.setattr("metrics.fetch_metrics", fake_fetch)
    val = await query_prometheus("sum(gateway_requests_total)")
    assert val == 42.0


async def test_query_prometheus_empty(monkeypatch):
    from metrics import query_prometheus
    async def fake_fetch():
        return {}
    monkeypatch.setattr("metrics.fetch_metrics", fake_fetch)
    assert await query_prometheus("anything") == 0.0


# ── fake MySQL pool helpers ──
# dashboard 三个端点改为查 MySQL calls 表，用 fake pool 替代真实连接

def _fake_pool(fetchone=None, fetchall=None, capture=None):
    """构建 fake get_pool() 供 dashboard MySQL 测试。

    fetchone: cursor.fetchone() 返回值
    fetchall: cursor.fetchall() 返回值
    capture:  可选 list，收集 (sql, args) 供断言 server filter 等
    """
    class Cur:
        description = []
        async def execute(self, sql, args=None):
            if capture is not None:
                capture.append((sql, args))
        async def fetchone(self):
            return fetchone
        async def fetchall(self):
            return fetchall or []
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    class Conn:
        def cursor(self): return Cur()  # aiomysql: cursor() 是同步方法返回 async CM
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
    class Pool:
        def acquire(self): return Conn()
    async def get_pool():
        return Pool()
    return get_pool


# ── /api/metrics/summary (MySQL) ──

def test_metrics_summary_from_mysql(client, auth_headers, monkeypatch):
    """summary 从 MySQL 聚合，不再查 Prometheus。"""
    # fetchone: (total, errors, avg_lat, max_lat, reads, writes)
    # fetchall: latency list for P95
    # fetchall: latency rows (MySQL cursor returns tuples)
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchone=(100, 5, 50, 200, 60, 40),
        fetchall=[(10,), (20,), (30,), (40,), (50,), (60,), (70,), (80,), (90,), (100,)],
    ))
    resp = client.get("/api/metrics/summary", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["requests"] == 100
    assert body["errors"] == 5
    assert body["error_rate"] == 5.0
    assert body["read"] == 60
    assert body["write"] == 40
    assert body["avg_ms"] == 50
    # 10 items, int(10*0.95)=9 -> lats[9]=100
    assert body["p95_ms"] == 100


def test_metrics_summary_empty(client, auth_headers, monkeypatch):
    """无数据时返回零值，不崩溃。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchone=(0, None, None, None, None, None),
        fetchall=[],
    ))
    resp = client.get("/api/metrics/summary", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["requests"] == 0
    assert body["errors"] == 0
    assert body["error_rate"] == 0.0
    assert body["p95_ms"] == 0
    assert body["avg_ms"] == 0


def test_metrics_summary_server_filter(client, auth_headers, monkeypatch):
    """server 参数必须传播到所有 SQL 查询的 WHERE + args。"""
    captured = []
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchone=(1, 0, 10, 20, 1, 0),
        fetchall=[(10,)],
        capture=captured,
    ))
    resp = client.get("/api/metrics/summary?server=zabbix", headers=auth_headers)
    assert resp.status_code == 200
    # 每条 SQL 都应含 server=%s 且 args 含 zabbix
    for sql, args in captured:
        assert "server=%s" in sql, f"server filter missing in SQL: {sql}"
        assert args == ["zabbix"], f"server arg missing: {args}"


# ── /api/metrics/by-server (MySQL) ──

def test_metrics_by_server_from_mysql(client, auth_headers, monkeypatch):
    """by-server 从 MySQL GROUP BY server 聚合。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchall=[("zabbix-mcp", 100, 5), ("tavily-mcp", 50, 0)],
    ))
    resp = client.get("/api/metrics/by-server", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["server"] == "zabbix-mcp"
    assert data[0]["requests"] == 100
    assert data[0]["errors"] == 5
    assert data[0]["error_rate"] == 5.0


def test_metrics_by_server_empty(client, auth_headers, monkeypatch):
    """无数据时返回空列表。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(fetchall=[]))
    resp = client.get("/api/metrics/by-server", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


# ── /api/metrics/timeseries (MySQL) ──

def test_metrics_timeseries_from_mysql(client, auth_headers, monkeypatch):
    """timeseries 按分钟桶 GROUP BY 聚合。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchall=[("2026-08-04 10:00:00", 100, 5), ("2026-08-04 10:01:00", 50, 0)],
    ))
    resp = client.get("/api/metrics/timeseries", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["time"] == "2026-08-04 10:00:00"
    assert data[0]["requests"] == 100
    assert data[0]["errors"] == 5


def test_metrics_timeseries_server_filter(client, auth_headers, monkeypatch):
    """timeseries server 参数传播到 WHERE。"""
    captured = []
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchall=[],
        capture=captured,
    ))
    resp = client.get("/api/metrics/timeseries?server=zabbix", headers=auth_headers)
    assert resp.status_code == 200
    sql, args = captured[0]
    assert "server=%s" in sql
    assert args == ["zabbix"]


# ── /api/failures (Redis Stream, 保留不动) ──

async def test_failures_from_stream(client, fake_redis, auth_headers):
    # seed a failure in the audit stream
    await fake_redis.xadd("audit:failures", {
        "trace": "abc", "server": "zabbix", "tool": "list", "op": "read",
        "error_type": "upstream_timeout", "message": "timeout", "latency_ms": "30",
        "time": "2026-07-30T12:00:00Z", "journey": "[]", "token_name": "ro",
    })
    resp = client.get("/api/failures?limit=10", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["trace"] == "abc"
    assert data[0]["error_type"] == "upstream_timeout"
