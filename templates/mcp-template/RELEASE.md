# {{MCP_NAME}} — 发布指南

## 版本管理

遵循 [SemVer](https://semver.org/)：
- `MAJOR`: breaking changes（tool 签名变更、删除 tool）
- `MINOR`: 新增功能（新 tool、新 resource）
- `PATCH`: bug fix、文档更新

## 发布流程

### 1. 本地验证

```bash
# 跑测试
uv run pytest tests/ -v

# 启动 server，手动验证
uv run python server.py
uv run python client.py

# 检查 protocol 兼容性
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"check","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}' | python3 -m json.tool
```

### 2. 更新版本

```bash
# 更新 pyproject.toml 中的 version
# 更新 README.md changelog
```

### 3. 构建 & 发布

```bash
# 构建
uv build

# 发布到 PyPI（需要先配置 token）
uv publish

# 或发布到私有 registry
uv publish --index <private-index>
```

### 4. 发布后检查

- [ ] PyPI 页面显示正确版本
- [ ] `pip install <package>` 能拉到新版
- [ ] README 渲染正常
- [ ] 依赖版本无冲突

## 部署到正式环境

### 环境要求

- Python >=3.12
- 支持 HTTP 的运行时（Docker / K8s / Serverless）

### Docker 部署

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY server.py .
CMD ["uv", "run", "python", "server.py"]
```

### 健康检查

```bash
curl -X POST http://<host>:<port>/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"health","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}'
```

### 可观测性

- 结构化日志 → stdout（key=value 格式）
- OpenTelemetry traces → 配置 `OTEL_EXPORTER_OTLP_ENDPOINT`
- 每个请求 SERVER span 自动创建

## Changelog

<!-- 每次发布追加 -->

### Unreleased

- 初始版本
