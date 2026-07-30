import pytest
import httpx
from mcp_probe import probe, introspect_tools, HealthResult


async def test_probe_up(monkeypatch):
    async def fake_post(self, url, json=None):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is True
    assert result.latency_ms is not None


async def test_probe_down(monkeypatch):
    async def fake_post(self, url, json=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is False
    assert result.latency_ms is None


async def test_introspect_tools(monkeypatch):
    async def fake_post(self, url, json=None):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
            "tools": [
                {"name": "list_items", "description": "list", "annotations": {"readOnlyHint": True}},
                {"name": "create_item", "description": "create", "annotations": {"destructiveHint": True}},
                {"name": "no_ann", "description": "no annotations"},
            ]
        }})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    tools = await introspect_tools("http://localhost:9999/mcp")
    assert len(tools) == 3
    assert tools[0]["mode"] == "read"
    assert tools[1]["mode"] == "write"
    assert tools[2]["mode"] == "read"  # default when no annotations


async def test_introspect_non_json(monkeypatch):
    async def fake_post(self, url, json=None):
        return httpx.Response(502, text="<html>Bad Gateway</html>")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    tools = await introspect_tools("http://localhost:9999/mcp")
    assert tools == []
