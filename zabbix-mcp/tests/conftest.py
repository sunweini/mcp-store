"""Shared test fixtures for Zabbix MCP tests.

Provides mock_zabbix fixture using httpx MockTransport,
avoiding any real Zabbix API calls during unit tests.
"""
import json
import pytest
import httpx

from zabbix_client import ZabbixClient


def make_jsonrpc_response(result, id=1):
    """Build a Zabbix JSON-RPC success response body."""
    return json.dumps({"jsonrpc": "2.0", "result": result, "id": id}).encode()


def make_jsonrpc_error(message, code=-32602, id=1):
    """Build a Zabbix JSON-RPC error response body."""
    return json.dumps({
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
        "id": id,
    }).encode()


@pytest.fixture
def mock_zabbix():
    """Create a ZabbixClient with a mock HTTP transport.

    Usage in tests:
        def test_something(mock_zabbix):
            mock_zabbix.enqueue_result([{"host": "web-01"}])
            result = await some_tool(..., zabbix=mock_zabbix)
    """
    client = ZabbixClient(url="http://mock-zabbix/api_jsonrpc.php", token="test-token")

    # Replace httpx client with one using MockTransport
    responses = []
    async def handler(request: httpx.Request) -> httpx.Response:
        if responses:
            body = responses.pop(0)
        else:
            body = make_jsonrpc_response([])
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._responses = responses  # test can append to this
    client.enqueue_result = lambda r: responses.append(make_jsonrpc_response(r))
    client.enqueue_error = lambda m, c=-32602: responses.append(make_jsonrpc_error(m, c))

    yield client


@pytest.fixture
def mock_zabbix_no_env(monkeypatch):
    """ZabbixClient that works without real env vars."""
    monkeypatch.setenv("ZABBIX_URL", "http://mock/api_jsonrpc.php")
    monkeypatch.setenv("ZABBIX_TOKEN", "test-token")
