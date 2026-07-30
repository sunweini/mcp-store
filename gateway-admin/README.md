# gateway-admin

MCP Gateway Admin - management API + Vue 3 UI.

FastAPI management plane for the MCP Gateway. Shares Redis with gateway-proxy;
writes servers/tokens/admin, proxy hot-reloads via Pub/Sub.

## Development

```bash
uv sync
REDIS_URL=redis://localhost:6379/0 JWT_SECRET=dev uv run uvicorn app:app --port 8081 --reload
uv run pytest tests/ -v
```

See `CLAUDE.md` for full configuration and architecture notes.
