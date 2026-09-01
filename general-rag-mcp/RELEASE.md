# general-rag-mcp — 发布指南

## 版本管理

遵循 [SemVer](https://semver.org/)：
- `MAJOR`: breaking changes（tool 签名变更、删除 tool）
- `MINOR`: 新增功能（新 tool、新 resource）
- `PATCH`: bug fix、文档更新

## 发布流程

### 1. 本地验证

```bash
uv run python -m pytest tests/ -q

# 启动 server，冒烟验证
uv run python server.py
uv run python client.py

# 检查 protocol 兼容性
curl -s -X POST http://127.0.0.1:9055/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"check","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}' | python3 -m json.tool
```

### 2. 构建镜像 & 部署

```bash
# 继承仓库基础镜像 mcp-base（见 deploy/Dockerfile.base）
cd ../deploy
docker compose build general-rag-mcp
docker compose up -d general-rag-mcp
```

### 3. 注册到 Gateway

1. 访问 `http://localhost:8081`（gateway-admin）
2. Servers 页添加：
   - Name: `general-rag-mcp`
   - URL: `http://general-rag-mcp:9055/mcp`（容器内网互访）
   - Description: `检索 general-rag 内部知识库（章管家接口文档等）`
3. 创建 API Token 并勾选本 MCP 的 read/write 权限
4. 若注册后 tools 为 0，用「refresh-tools」修复（竞态已知问题）

## 环境要求

- Python >=3.12
- 后端 general-rag 网络可达（`GENERAL_RAG_BASE_URL`）

## 可观测性

- 结构化日志 → stdout（key=value，`LOG_FORMAT=json` 切 JSON）
- OpenTelemetry traces → 配置 `OTEL_EXPORTER_OTLP_ENDPOINT`
- Prometheus metrics → `/metrics`（`PROMETHEUS_PORT`，默认 9464）

## Changelog

### 0.1.0（2026-09-01）

- 初始版本：search / namespaces / health / ingest 四工具
