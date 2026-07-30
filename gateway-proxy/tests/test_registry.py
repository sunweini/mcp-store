"""Tests for server registry: probe + parse_change_event."""
import pytest
from registry import probe, parse_change_event


async def test_probe_up(fake_redis, monkeypatch):
    # probe hits a URL with MCP ping; mock httpx to return 200
    import httpx
    async def fake_post(self, url, json=None):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is True
    assert result.latency_ms >= 0


async def test_probe_down(monkeypatch):
    import httpx
    async def fake_post(self, url, json=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is False


def test_parse_change_event_add():
    evt = parse_change_event('{"action":"add","name":"zabbix"}')
    assert evt == ("add", "zabbix")


def test_parse_change_event_invalid():
    assert parse_change_event("not json") is None
