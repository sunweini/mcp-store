# Zabbix MCP — 发布指南

## 版本管理

遵循 SemVer：
- MAJOR: breaking changes（tool 签名变更、删除 tool）
- MINOR: 新增 tool
- PATCH: bug fix、文档

## 发布流程

### 1. 本地验证

```bash
uv run pytest tests/ -v
uv run python server.py  # 手动验证
```

### 2. 更新版本

编辑 `pyproject.toml` 中 `version` 字段。

### 3. 构建 & 发布

```bash
uv build
uv publish
```

## Docker 部署

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY server.py zabbix_client.py ./
COPY tools/ tools/
CMD ["uv", "run", "python", "server.py"]
```

## 健康检查

```bash
curl -X POST http://<host>:9053/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"health","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}'
```

## Changelog

### Unreleased
- 初始版本：9 个 tool（problems/maintenance/events）
