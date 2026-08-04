# SerpAPI MCP — 发布指南

## 版本管理

遵循 SemVer：
- MAJOR: breaking changes（tool 签名变更、删除 tool）
- MINOR: 新增 tool
- PATCH: bug fix、文档

## 发布流程

### 1. 本地验证

```bash
uv sync --all-extras   # 必须 --all-extras，否则 venv 无 pytest
redis-server --daemonize yes   # KeyPool 依赖本地 Redis
uv run pytest tests/ -v
uv run python server.py   # 手动验证，REDIS_URL 必填
```

### 2. 更新版本

编辑 `pyproject.toml` 中 `version` 字段。

### 3. 构建 & 发布

```bash
uv build
uv publish
```

## Docker 部署

继承仓库基础镜像 `mcp-base`（python3.12-slim + uv + 阿里云 apt/pypi 镜像源，
见 `deploy/Dockerfile.base`）——生产网络受限，apt/pypi 走阿里云，uv 二进制
来自 ghcr（可达）：

```dockerfile
FROM mcp-base:latest
WORKDIR /app

# uv.lock 与 pyproject 一致（uv lock --check 通过），--frozen 保证可复现构建
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev

COPY . ./

ENV MCP_HOST=0.0.0.0
CMD ["uv", "run", "python", "server.py"]
```

容器内端口 **9052**（仓库规范 9050-9500，见根 CLAUDE.md）。与 gateway-proxy
同网段，**不映射宿主端口**；metrics 如需采集宿主映射 9464 起错开
（同机多 MCP 时 9466/9467/9468 已占用，按需递增）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `REDIS_URL` | 无（必填） | Redis 连接（key 池存储 + 热更新 pubsub） |
| `MCP_HOST` | `127.0.0.1` | 监听地址（Dockerfile 设 `0.0.0.0`） |
| `MCP_PORT` | `9052` | MCP 端口 |
| `SERPAPI_QUOTA_DEFAULT` | `100` | 未设 monthly_quota 时默认月配额 |
| `PROMETHEUS_PORT` | `9464` | Prometheus /metrics 端口（同机多 MCP 需错开） |
| `LOG_FORMAT` | `console` | `console`（开发）/ `json`（生产） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 无 | OTLP collector URL（未设则 console span） |
| `OTEL_SERVICE_NAME` | `serpapi-mcp` | 服务名（trace/metrics label） |

**Key 管理**：key 存 Redis `search:keys:serpapi`，由 gateway-admin 前台
API Keys 页维护（编辑后自动 publish 热重载），**不在容器配 key env**。

> **配额最紧张（月配额默认 100 次）**：gateway 每 30s 的探活是标准 MCP
> ping，不消耗配额；但 gateway-admin **添加/重新添加 key 时会发一次真实
> 搜索探活**（`_probe_key`，消耗 1 次额度），且探活结果计入官方计数。
> 避免频繁删加重加 key——额度按 `search:usage:serpapi:<key_id>` 月窗口
> 统计，重加后剩余额度归零重计。

## 工具清单

5 个引擎的只读搜索工具（全部 `readOnlyHint=True`，无长任务）：

| Tool | 引擎 | 重试 | 超时 | 说明 |
|---|---|---|---|---|
| `serpapi_google` | google | 换 key 重试 1 次 | 10s | Google Web 搜索（gl/hl/num/start） |
| `serpapi_bing` | bing | 换 key 重试 1 次 | 10s | Bing 搜索（gl/hl/cc/count） |
| `serpapi_baidu` | baidu | 换 key 重试 1 次 | 10s | 百度搜索（cti/page_num） |
| `serpapi_duckduckgo` | duckduckgo | 换 key 重试 1 次 | 10s | DuckDuckGo（kl） |
| `serpapi_ebay` | ebay | 换 key 重试 1 次 | 10s | eBay 商品搜索（_nkw/ebay_domain） |

## 健康检查

```bash
curl -X POST http://<host>:9052/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"health","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Gateway 每 30s 探活。

## Changelog

### Unreleased
- 初始版本：5 个引擎 tool（google/bing/baidu/duckduckgo/ebay），KeyPool 多 key 轮换 + 故障转移
