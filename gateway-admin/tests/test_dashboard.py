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
    # mock prometheus queries
    import metrics
    async def fake_query(q):
        return {"sum(gateway_requests_total)": 100, "sum(gateway_auth_failures_total)": 2}.get(q, 0)
    monkeypatch.setattr(metrics, "query_prometheus", fake_query)
    resp = client.get("/api/metrics/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["requests"] == 100
    assert data["auth_failures"] == 2


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
