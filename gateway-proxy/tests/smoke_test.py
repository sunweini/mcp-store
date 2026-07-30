"""Smoke test: start the gateway with a fake Redis, curl tools/list.

Patches redis_client._redis with fakeredis before importing server, so no
real Redis is needed. Starts the FastMCP HTTP app via uvicorn directly.

Usage:
    uv run python tests/smoke_test.py
"""
import asyncio
import json
import sys

import httpx
import fakeredis.aioredis

# Patch Redis BEFORE importing server modules that use it.
import redis_client

fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
redis_client._redis = fake

# Now import server (it will use the patched Redis).
import server  # noqa: E402


async def main():
    """Start the gateway HTTP app, curl tools/list, verify empty result."""
    # json_response=True returns plain JSON instead of SSE frames, which is
    # easier to parse in a smoke test. The production server uses the
    # default SSE transport for MCP protocol compliance.
    app = server.gateway.http_app(stateless_http=True, json_response=True)

    async with server.gateway._lifespan_manager():
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=18080, log_level="warning")
        srv = uvicorn.Server(config)

        server_task = asyncio.create_task(srv.serve())
        # Give uvicorn a moment to bind.
        await asyncio.sleep(1.5)

        try:
            # Curl tools/list (no auth header - tools/list doesn't require auth).
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "http://127.0.0.1:18080/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                print(f"HTTP status: {resp.status_code}")
                body = resp.json()
                print(f"Response: {json.dumps(body, indent=2)}")

                # Verify it's a valid JSON-RPC response with empty tools.
                assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
                assert "result" in body, f"Expected 'result' key, got: {body}"
                tools = body["result"].get("tools", [])
                assert tools == [], f"Expected empty tools list, got: {tools}"
                print("\nSMOKE TEST PASSED: tools/list returned empty list, no error.")
        finally:
            srv.should_exit = True
            await asyncio.sleep(0.5)
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
