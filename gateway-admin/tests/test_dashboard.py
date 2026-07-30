"""Tests for Dashboard API: Prometheus metrics + failures Stream."""
import json
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


async def test_query_prometheus(monkeypatch):
    import httpx
    from metrics import query_prometheus
    async def fake_get(self, url, params=None):
        return httpx.Response(200, json={
            "status": "success",
            "data": {"result": [{"value": ["123", "42"]}]},
        })
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    val = await query_prometheus("sum(gateway_requests_total)")
    assert val == 42.0


async def test_query_prometheus_empty(monkeypatch):
    import httpx
    from metrics import query_prometheus
    async def fake_get(self, url, params=None):
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await query_prometheus("anything") == 0.0


def test_metrics_summary(client, fake_redis, auth_headers, monkeypatch):
    # mock prometheus queries — use substring match since queries now include server labels
    import metrics
    async def fake_query(q):
        if "gateway_requests_total" in q and "status!=" not in q and "operation=" not in q:
            return 100  # requests (no error/op filter)
        if "auth_failures" in q:
            return 2
        if "status!=" in q:
            return 5    # errors
        if "operation=\"read\"" in q:
            return 60   # reads
        if "operation=\"write\"" in q:
            return 40   # writes
        if "duration_seconds" in q:
            return 0.05 # p95 = 50ms
        return 0
    monkeypatch.setattr(metrics, "query_prometheus", fake_query)
    resp = client.get("/api/metrics/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["requests"] == 100
    assert data["auth_failures"] == 2
    assert data["errors"] == 5
    assert data["read"] == 60
    assert data["write"] == 40
    assert data["p95_ms"] == 50.0


def test_metrics_summary_server_filter(client, fake_redis, auth_headers, monkeypatch):
    """Server filter must propagate to ALL PromQL queries (errors, reads, writes, p95)."""
    import metrics
    captured: list[str] = []
    async def fake_query(q):
        captured.append(q)
        return 1.0
    monkeypatch.setattr(metrics, "query_prometheus", fake_query)
    resp = client.get("/api/metrics/summary?server=zabbix", headers=auth_headers)
    assert resp.status_code == 200

    # Every query that touches gateway_requests_total or duration_seconds must carry server="zabbix"
    for q in captured:
        if "gateway_requests_total" in q or "duration_seconds" in q:
            assert 'server="zabbix"' in q, f"server filter missing in query: {q}"

    # auth_failures should NOT get a server filter (it has no server label)
    auth_q = [q for q in captured if "auth_failures" in q][0]
    assert 'server=' not in auth_q, f"auth_failures should not be server-filtered: {auth_q}"


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
