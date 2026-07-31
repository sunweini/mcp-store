import json
import httpx
from mcp_probe import probe, introspect_tools


def _sse(payload: dict) -> str:
    """Wrap a JSON-RPC payload as the SSE frame FastMCP streamable-http emits.

    Why: exercises the ``data:`` extraction branch in ``_parse_payload`` so the
    tests prove introspection works against real backend transport output, not
    just the plain-JSON fallback path.
    """
    return f"event: message\ndata: {json.dumps(payload)}\n\n"


async def test_probe_up(monkeypatch):
    sent_headers = {}

    async def fake_post(self, url, json=None, headers=None):
        sent_headers.update(headers or {})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is True
    assert result.latency_ms is not None
    # Why: probe must negotiate both transports in ``Accept``; an SSE-only
    # backend would otherwise 406 the request and look falsely offline.
    assert "text/event-stream" in sent_headers.get("Accept", "")


async def test_probe_down(monkeypatch):
    async def fake_post(self, url, json=None, headers=None):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is False
    assert result.latency_ms is None


async def test_probe_error_frame(monkeypatch):
    # Why: FastMCP answers errored JSON-RPC calls with HTTP 200, so probe must
    # reject payloads carrying an ``error`` key instead of trusting the status.
    async def fake_post(self, url, json=None, headers=None):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32600}})
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is False


async def test_probe_sse_frame(monkeypatch):
    async def fake_post(self, url, json=None, headers=None):
        return httpx.Response(200, text=_sse({"jsonrpc": "2.0", "id": 1, "result": {}}))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    result = await probe("http://localhost:9999/mcp")
    assert result.up is True
    assert result.latency_ms is not None


async def test_introspect_tools(monkeypatch):
    async def fake_post(self, url, json=None, headers=None):
        return httpx.Response(200, text=_sse({"jsonrpc": "2.0", "id": 1, "result": {
            "tools": [
                {"name": "list_items", "description": "list", "annotations": {"readOnlyHint": True}},
                {"name": "create_item", "description": "create", "annotations": {"destructiveHint": True}},
                {"name": "no_ann", "description": "no annotations"},
            ]
        }}))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    tools = await introspect_tools("http://localhost:9999/mcp")
    assert len(tools) == 3
    assert tools[0]["mode"] == "read"
    assert tools[1]["mode"] == "write"
    assert tools[2]["mode"] == "read"  # default when no annotations


async def test_introspect_non_json(monkeypatch):
    async def fake_post(self, url, json=None, headers=None):
        return httpx.Response(502, text="<html>Bad Gateway</html>")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    tools = await introspect_tools("http://localhost:9999/mcp")
    assert tools == []
