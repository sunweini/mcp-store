"""Test client for general-rag-mcp.

Directly connects to the local server (no gateway), lists tools, and runs a
sample search + namespaces + health call. Used for local smoke testing.

Usage:
    uv run python client.py
"""
import asyncio
import json

from fastmcp.client import Client


async def main() -> None:
    async with Client("http://127.0.0.1:9055/mcp") as client:
        tools = await client.list_tools()
        print("=== tools ===")
        for t in tools:
            print(f"  {t.name}: {t.description!r}")

        print("\n=== namespaces ===")
        r = await client.call_tool("knowledge_base_namespaces", {})
        print(json.dumps(r.data, ensure_ascii=False, indent=2))

        print("\n=== health ===")
        r = await client.call_tool("knowledge_base_health", {})
        print(json.dumps(r.data, ensure_ascii=False, indent=2))

        print("\n=== search ===")
        r = await client.call_tool(
            "knowledge_base_search",
            {"query": "怎么获取token accessToken超时怎么办", "namespace": "stamp-project", "top_k": 3},
        )
        print(json.dumps(r.data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
