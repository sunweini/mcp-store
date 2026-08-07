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

def _fake_pool(fetchone=None, fetchall=None, fetchall_seq=None, capture=None):
    """构建 fake get_pool() 供 dashboard MySQL 测试。

    fetchone:     cursor.fetchone() 返回值（所有 execute 共用）
    fetchall:     cursor.fetchall() 返回值（所有 execute 共用）
    fetchall_seq: 按 execute 顺序依次返回的 fetchall 列表（多查询场景）
    capture:      可选 list，收集 (sql, args) 供断言 server filter 等
    """
    class Cur:
        description = []
        def __init__(self):
            self._idx = -1
        async def execute(self, sql, args=None):
            self._idx += 1
            if capture is not None:
                capture.append((sql, args))
        async def fetchone(self):
            return fetchone
        async def fetchall(self):
            if fetchall_seq is not None:
                if 0 <= self._idx < len(fetchall_seq):
                    return fetchall_seq[self._idx]
                return []
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
    # fetchone: (total, errors, avg_lat, max_lat, reads, writes, auth_failures)
    # fetchall: latency rows (MySQL cursor returns tuples)
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchone=(100, 5, 50, 200, 60, 40, 3),
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
    assert body["auth_failures"] == 3
    assert body["avg_ms"] == 50
    # 10 items, int(10*0.95)=9 -> lats[9]=100
    assert body["p95_ms"] == 100


def test_metrics_summary_empty(client, auth_headers, monkeypatch):
    """无数据时返回零值，不崩溃。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchone=(0, None, None, None, None, None, None),
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
    assert body["auth_failures"] == 0


def test_metrics_summary_server_filter(client, auth_headers, monkeypatch):
    """server 参数必须传播到所有 SQL 查询的 WHERE + args。"""
    captured = []
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchone=(1, 0, 10, 20, 1, 0, 0),
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
    """by-server 从 MySQL GROUP BY server 聚合，含 P95 + error_rate。"""
    # query 1 (GROUP BY): [(server, count, errors), ...]
    # query 2 (latencies): [(server, latency), ...] sorted by server, latency
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchall_seq=[
            [("zabbix-mcp", 100, 5), ("tavily-mcp", 50, 0)],
            # zabbix: lats=[10,20,30], int(3*0.95)=2 -> p95=30
            # tavily: lats=[5,10], int(2*0.95)=1 -> p95=10
            [("zabbix-mcp", 10), ("zabbix-mcp", 20), ("zabbix-mcp", 30),
             ("tavily-mcp", 5), ("tavily-mcp", 10)],
        ],
    ))
    resp = client.get("/api/metrics/by-server", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["server"] == "zabbix-mcp"
    assert data[0]["requests"] == 100
    assert data[0]["errors"] == 5
    assert data[0]["error_rate"] == 5.0
    assert data[0]["p95_ms"] == 30
    assert data[1]["server"] == "tavily-mcp"
    assert data[1]["p95_ms"] == 10


def test_metrics_by_server_empty(client, auth_headers, monkeypatch):
    """无数据时返回空列表。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(fetchall=[]))
    resp = client.get("/api/metrics/by-server", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


# ── /api/metrics/timeseries (MySQL) ──

def test_metrics_timeseries_format(client, auth_headers, monkeypatch):
    """timeseries 返回 {window, points: [float]} 保持前端兼容。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(fetchall=[]))
    resp = client.get("/api/metrics/timeseries", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"] == "1h"
    assert "points" in body
    assert isinstance(body["points"], list)
    # 1h 窗口 = 60 分钟桶
    assert len(body["points"]) == 60
    # 无数据 -> 全 0
    assert all(p == 0.0 for p in body["points"])


def test_metrics_timeseries_24h(client, auth_headers, monkeypatch):
    """24h 窗口 = 24 小时桶。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(fetchall=[]))
    resp = client.get("/api/metrics/timeseries?window=24h", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"] == "24h"
    assert len(body["points"]) == 24


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


# ── /api/failures (MySQL calls 表) ──
# 失败面板数据源统一到 MySQL：与总请求/错误数同源。calls 表由审计消费者
# XREADGROUP audit:calls 批量落库，admin 只读 MySQL（旧 Stream 测试随之移除）

def _fail_row(trace="abc", server="zabbix", tool="list", op="read",
              error_type="upstream_timeout", message="timeout",
              latency_ms=30, when=None, journey='[{"stage": "auth", "state": "fail", "ms": 30}]'):
    """构造 calls 表 SELECT 顺序的行元组（time 是 DATETIME -> datetime 对象）。"""
    from datetime import datetime
    return (trace, server, tool, op, error_type, message, latency_ms,
            when or datetime(2026, 8, 5, 2, 0, 0), journey)


def test_failures_from_mysql(client, auth_headers, monkeypatch):
    """failures 从 MySQL calls 表读，journey JSON 解析为 list，结构与旧 Redis 版一致。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(fetchall=[_fail_row()]))
    resp = client.get("/api/failures?limit=10", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    rec = data[0]
    assert rec["trace"] == "abc"
    assert rec["server"] == "zabbix"
    assert rec["tool"] == "list"
    assert rec["op"] == "read"
    assert rec["error_type"] == "upstream_timeout"
    assert rec["message"] == "timeout"
    assert rec["latency_ms"] == 30
    # journey 必须是解析后的 list（前端「查看轨迹」直接消费）
    assert rec["journey"] == [{"stage": "auth", "state": "fail", "ms": 30}]
    # time 保持 Redis 时代的 ISO+Z 格式（前端 ago() 按 UTC 解析）
    assert rec["time"] == "2026-08-05T02:00:00Z"


def test_failures_mysql_query_shape(client, auth_headers, monkeypatch):
    """SQL 必须参数化：status 过滤 + LIMIT/OFFSET 占位符，server 未传不进 WHERE。"""
    captured = []
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(fetchall=[], capture=captured))
    resp = client.get("/api/failures?limit=20&offset=5", headers=auth_headers)
    assert resp.status_code == 200
    sql, args = captured[0]
    assert "status = %s" in sql
    assert "ORDER BY id DESC" in sql
    assert args == ["fail", 20, 5]
    assert "server" not in sql.split("WHERE")[1].split("ORDER")[0].replace("server = %s", "")


def test_failures_server_filter(client, auth_headers, monkeypatch):
    """server 参数传播到 WHERE + args。"""
    captured = []
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(fetchall=[], capture=captured))
    resp = client.get("/api/failures?server=zabbix-mcp", headers=auth_headers)
    assert resp.status_code == 200
    sql, args = captured[0]
    assert "server = %s" in sql
    assert args == ["fail", "zabbix-mcp", 50, 0]


def test_failures_bad_journey_json(client, auth_headers, monkeypatch):
    """journey 列脏数据（非法 JSON）-> 返回 []，不让面板 500。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(
        fetchall=[_fail_row(journey="not-json{{")]))
    resp = client.get("/api/failures", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()[0]["journey"] == []


def test_failures_empty(client, auth_headers, monkeypatch):
    """无失败行 -> 空列表。"""
    monkeypatch.setattr("api.dashboard.get_pool", _fake_pool(fetchall=[]))
    resp = client.get("/api/failures", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []
