# Aliyun DNS MCP — 发布指南

## 版本管理

遵循 [SemVer](https://semver.org/)：
- `MAJOR`: breaking changes（tool 签名变更、删除 tool）
- `MINOR`: 新增功能（新 tool、新 resource）
- `PATCH`: bug fix、文档更新

## 发布流程

### 1. 本地验证

```bash
# 跑全量测试（注意：必须 python -m pytest，管道 tail 可能挂）
uv run python -m pytest tests/ -q

# 启动 server，冒烟验证
redis-server --daemonize yes
REDIS_URL=redis://localhost:6379/0 uv run python server.py
# 预期日志: account_store_loaded + otel_metrics_configured service=aliyun-dns-mcp port=9464

# 检查 protocol 兼容性
curl -s -X POST http://127.0.0.1:9054/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"check","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}' | python3 -m json.tool
# 预期 tools 6 个: list_accounts / list_domains / list_records / add_record / update_record / delete_record
```

### 2. 更新版本

```bash
# 更新 pyproject.toml 中的 version
# 更新 README.md changelog
```

### 3. 构建 & 发布

本 MCP 为内部 server（不发布 PyPI），部署走 Docker 镜像：

```bash
docker build -t aliyun-dns-mcp:<version> .
docker push <registry>/aliyun-dns-mcp:<version>
```

### 4. 发布后检查

- [ ] 镜像构建成功（uv.lock 必须阿里云镜像源，见下）
- [ ] gateway-admin 注册的 server 探活正常
- [ ] 账户授权矩阵保存后热更新生效（无需重启 MCP）
- [ ] 全量测试通过

## 部署到正式环境

### 环境要求

- Python >=3.12
- Redis（10.33.17.72 内网已有实例）
- Docker / K8s

### ⚠️ uv.lock 必须用阿里云镜像（生产构建前提）

生产服务器**无法访问 files.pythonhosted.org**。uv.lock 里 URL 写死官方源会导致生产构建失败。**新建/改依赖后必须重建 lock 指向阿里云**：

```bash
rm -f uv.lock
UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock
# 验证: grep -c mirrors.aliyun.com uv.lock 应 >0; files.pythonhosted.org 应为 0
```

### Docker 部署

```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY server.py aliyun_client.py auth.py account_store.py logging_config.py telemetry.py ./
COPY tools/ ./tools/
CMD ["uv", "run", "python", "server.py"]
```

### compose（并入 gateway compose）

```yaml
aliyun-dns-mcp:
  image: <registry>/aliyun-dns-mcp:<version>
  environment:
    REDIS_URL: redis://redis:6379/0
    MCP_PORT: "9054"            # 容器内固定，不映射宿主
    PROMETHEUS_PORT: "9464"     # 容器内固定；宿主端错开如 9469
    LOG_FORMAT: json
    OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
  networks: [gateway-net]       # 与 gateway-proxy 同网，供其内网访问
  depends_on: [redis]
  healthcheck:
    test: ["CMD", "curl", "-f", "-X", "POST", "http://127.0.0.1:9054/mcp",
           "-H", "MCP-Protocol-Version: 2026-07-28", "-H", "Mcp-Method: tools/list",
           "-d", "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{\"_meta\":{\"io.modelcontextprotocol/protocolVersion\":\"2026-07-28\",\"io.modelcontextprotocol/clientInfo\":{\"name\":\"health\",\"version\":\"1.0\"},\"io.modelcontextprotocol/clientCapabilities\":{}}}}"]
    interval: 30s
    timeout: 5s
    retries: 3
```

### 部署步骤（10.33.17.72）

1. 本地：`UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock` 重建 lock，`docker build -t aliyun-dns-mcp:<version> .`，push 到私有 registry
2. 服务器：拉镜像，更新 compose 中 version
3. gateway-admin → Servers：注册 `aliyun-dns-mcp`（URL `http://aliyun-dns-mcp:9054/mcp`）
4. gateway-admin → Tokens：为 token 勾选 `aliyun-dns-mcp` read/write
5. gateway-admin → 账户授权：为 token 配置账户级 read/write（Redis 写 + `aliyndns:changed` pubsub 热更新，MCP 无需重启）

### 健康检查

```bash
curl -X POST http://<host>:9054/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"health","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}'
```

### 可观测性

- 结构化日志 → stdout（key=value 格式，`LOG_FORMAT=json` 切 JSON 对接 Loki/ELK）
- Prometheus metrics → 容器内 `:9464/metrics`（宿主映射错开端口）
- OpenTelemetry traces → `OTEL_EXPORTER_OTLP_ENDPOINT`（未设时 console span）

## 已知注意事项

- **network_error 兜底**（spec §7.1）：工具层只捕获 `AlidnsError`。`AliyunClient._call` 的 `except Exception` 已把所有 SDK 异常（含网络/超时）包成 `AlidnsError`（不匹配分类时落 `api_error`），网络错误不会 500，但 error_type 非精确 `network_error`；Redis 连接异常与意外 bug 冒泡由 FastMCP 转 is_error。小规模 + 内网部署风险低，保持现状
- **httpx WARNING 防线**：httpx logger 提到 WARNING（SDK RPC URL query 含 AccessKeyId）。防线在 `logging_config.configure_logging`，任何不走它的进程会失去防线

## Changelog

<!-- 每次发布追加 -->

### Unreleased

- 初始版本：server 装配 + 6 tools + 账户级权限 + Redis 热更新 + telemetry/logging + 文档三件套
