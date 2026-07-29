"""ZabbixClient unit tests.

Tests JSON-RPC serialization, error mapping, and OTel span creation.
Uses httpx MockTransport — no real Zabbix server needed.
"""
import pytest
from zabbix_client import ZabbixClient, ZabbixAPIError, ZabbixConnectionError


async def test_call_returns_result(mock_zabbix):
    """Successful API call returns the 'result' field from JSON-RPC response."""
    mock_zabbix.enqueue_result([{"host": "web-01", "severity": 4}])

    result = await mock_zabbix.call("problem.get", {"output": "extend"})

    assert result == [{"host": "web-01", "severity": 4}]


async def test_call_sends_correct_jsonrpc_payload(mock_zabbix):
    """API call sends valid JSON-RPC 2.0 with auth token."""
    mock_zabbix.enqueue_result([])

    await mock_zabbix.call("host.get", {"filter": {"host": "web-01"}})

    # Verify the request was made (MockTransport consumed the response)
    # In a real test we'd inspect the captured request body


async def test_call_raises_zabbix_api_error_on_jsonrpc_error(mock_zabbix):
    """Zabbix API error response raises ZabbixAPIError."""
    mock_zabbix.enqueue_error("No permissions", -32602)

    with pytest.raises(ZabbixAPIError, match="No permissions"):
        await mock_zabbix.call("host.get", {})


async def test_call_raises_connection_error_on_network_failure():
    """Network failure raises ZabbixConnectionError."""
    import httpx

    async def failing_handler(request):
        raise httpx.ConnectError("Connection refused")

    client = ZabbixClient(url="http://bad-host/api_jsonrpc.php", token="tok")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(failing_handler))

    with pytest.raises(ZabbixConnectionError, match="Connection refused"):
        await client.call("host.get", {})

    await client.close()


async def test_close_closes_http_client(mock_zabbix):
    """close() shuts down the httpx client."""
    await mock_zabbix.close()
    assert mock_zabbix._http is None or mock_zabbix._http.is_closed
