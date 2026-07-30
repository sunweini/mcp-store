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


async def probe(url: str) -> HealthResult:
    """Ping a backend MCP server. 5s timeout."""
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}
            })
            return HealthResult(
                up=resp.status_code == 200,
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
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}
            })
            data = resp.json()
            tools = []
            for t in data.get("result", {}).get("tools", []):
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
