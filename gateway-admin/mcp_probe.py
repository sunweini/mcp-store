"""MCP backend probing + tools introspection.

probe() sends MCP ping (liveness). introspect_tools() calls tools/list
and classifies each tool read/write via annotations.destructiveHint.
Mirrors gateway-proxy's registry logic so admin UI has data immediately.
"""
import time
import json
import httpx
import structlog
from dataclasses import dataclass

logger = structlog.get_logger()


@dataclass
class HealthResult:
    up: bool
    latency_ms: float | None


async def _call_mcp(client: httpx.AsyncClient, url: str, method: str) -> tuple[httpx.Response, dict]:
    """Send a stateless MCP JSON-RPC request and parse the response.

    FastMCP streamable-http returns SSE frames (``event: message\\ndata: {...}``)
    unless the client negotiates plain JSON. We always send ``Accept`` for both
    and parse whichever shape comes back, so introspection works regardless of
    the backend's transport mode.
    """
    resp = await client.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    return resp, _parse_payload(resp.text)


def _parse_payload(text: str) -> dict:
    """Extract the JSON-RPC payload from an SSE frame or a plain JSON body."""
    data_lines = [ln[5:].lstrip() for ln in text.splitlines() if ln.startswith("data:")]
    if data_lines:
        try:
            return json.loads("".join(data_lines))
        except json.JSONDecodeError:
            return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


async def probe(url: str) -> HealthResult:
    """Ping a backend MCP server. 5s timeout."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp, payload = await _call_mcp(client, url, "ping")
            # OBS: 200 alone is not enough — FastMCP returns 200 for error frames too,
            # so also confirm the JSON-RPC result object is present (no ``error`` key).
            ok = resp.status_code == 200 and "result" in payload and "error" not in payload
            return HealthResult(
                up=ok,
                latency_ms=round((time.monotonic() - start) * 1000, 1),
            )
    except httpx.HTTPError:
        return HealthResult(up=False, latency_ms=None)


async def introspect_tools(url: str) -> list[dict]:
    """Call tools/list, return [{name, mode, description}].

    mode is 'write' if annotations.destructiveHint else 'read' (default).
    Returns [] on any error (non-JSON, connection, etc).
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            _, payload = await _call_mcp(client, url, "tools/list")
            tools = []
            for t in payload.get("result", {}).get("tools", []):
                ann = t.get("annotations") or {}
                tools.append({
                    "name": t.get("name", ""),
                    "mode": "write" if ann.get("destructiveHint") else "read",
                    "description": t.get("description", ""),
                })
            return tools
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as e:
        logger.warning("introspect_failed", url=url, error=str(e), service="gateway-admin")
        return []
