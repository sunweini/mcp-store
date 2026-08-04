"""Tests for /api/calls - 请求明细（MySQL calls 表）。"""
import pytest
from auth import create_jwt


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {create_jwt('admin')}"}


def test_list_calls_requires_auth(client):
    assert client.get("/api/calls").status_code == 401


def test_list_calls_empty(client, auth_headers, monkeypatch):
    """空表 -> count 0。"""
    async def fake_query(sql, args=None):
        return []
    monkeypatch.setattr("api.calls.query_calls", lambda **kw: _coro([]))
    resp = client.get("/api/calls", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "data": []}


async def _coro(v): return v


def test_list_calls_returns_rows(client, auth_headers, monkeypatch):
    rows = [
        {"id": 2, "time": "2026-08-04 10:00:01", "server": "serpapi-mcp",
         "tool": "serpapi_baidu", "op": "read", "token_name": "b",
         "latency_ms": 5, "status": "fail", "error_type": "upstream_error", "trace": "t2"},
        {"id": 1, "time": "2026-08-04 10:00:00", "server": "tavily-mcp",
         "tool": "tavily_search", "op": "read", "token_name": "a",
         "latency_ms": 42, "status": "ok", "error_type": "", "trace": "t1"},
    ]
    monkeypatch.setattr("api.calls.query_calls", lambda **kw: _coro(rows))
    resp = client.get("/api/calls", headers=auth_headers)
    body = resp.json()
    assert body["count"] == 2
    assert body["data"][0]["trace"] == "t2"
