# serpapi-mcp

SerpAPI 搜索 MCP server：多 key 池（Redis 驱动）自动轮换与故障转移，
为 AI agent 提供 Google / Bing / 百度 / DuckDuckGo / eBay 5 个引擎的
搜索能力。全部工具只读。

## 功能

| Tool | 引擎 | 说明 |
|---|---|---|
| `serpapi_google` | Google | Web 搜索（gl/hl 国家语言 / num 结果数 / start 分页） |
| `serpapi_bing` | Bing | Web 搜索（gl/hl/cc / count） |
| `serpapi_baidu` | 百度 | Web 搜索（cti 时间过滤 / page_num 页码） |
| `serpapi_duckduckgo` | DuckDuckGo | Web 搜索（kl 区域语言） |
| `serpapi_ebay` | eBay | 商品搜索（_nkw 关键词 / ebay_domain 站点） |

响应返回 SerpAPI 原始 JSON（`organic_results` / `shopping_results`），
工具层不做重结构化（官方 mcp-serpapi 同策略）。

## 快速开始

### 连接配置

```json
{
  "mcpServers": {
    "serpapi": {
      "url": "http://localhost:9052/mcp"
    }
  }
}
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `REDIS_URL` | 无（必填） | Redis 连接（key 池存储 + 热更新 pubsub） |
| `MCP_HOST` | `127.0.0.1` | 监听地址 |
| `MCP_PORT` | `9052` | MCP 端口 |
| `LOG_FORMAT` | `console` | `console`（开发）/ `json`（生产） |
| `SERPAPI_QUOTA_DEFAULT` | `100` | 未设 monthly_quota 时默认月配额 |
| `PROMETHEUS_PORT` | `9464` | Prometheus /metrics 端口（与 zabbix/tavily/brave 同机部署会冲突，部署时需错开） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 无 | OTLP collector URL（未设则 console span） |
| `OTEL_SERVICE_NAME` | `serpapi-mcp` | 服务名（trace/metrics label） |

### 从源码运行

```bash
git clone <repo>
cd serpapi-mcp
uv sync --all-extras
export REDIS_URL="redis://localhost:6379/0"
uv run python server.py
```

### 运行测试

```bash
cd serpapi-mcp && uv run pytest tests/ -v
```

### Docker

```bash
docker run -p 9052:9052 -e REDIS_URL=redis://host.docker.internal:6379/0 \
  -e MCP_HOST=0.0.0.0 -e MCP_PORT=9052 <image>
```

## API Key 管理

Key 池存放在 Redis `search:keys:serpapi`，由 **gateway-admin 的 API Keys
页面**统一管理（添加 / 启用停用 / 查看配额）。管理员改动后 server 通过
pubsub 热更新自动生效，无需重启。

Key 记录字段：

| 字段 | 说明 |
|---|---|
| `key` | SerpAPI API key（URL query 参数 `api_key`） |
| `monthly_quota` | 月配额（未设则用 `SERPAPI_QUOTA_DEFAULT`，默认 100） |
| `status` | `active` / `invalid` / `exhausted` / `cooldown` / `low_quota` |
| `remaining` | 剩余配额（错误或用量校验时更新） |
| `enabled` | 手动停用开关 |

## 错误语义

| HTTP / 响应 | 处理 |
|---|---|
| 401 | key 失效 → 永久剔除（INVALID） |
| 429 | 限流 → 冷却 30s（恒用默认，Retry-After 头未解析；RATE_LIMIT） |
| 200 + body 含 `account has exceeded quota` / `quota exceeded` / `insufficient credits` | 欠费 → 永久剔除（EXHAUSTED） |
| 其余 4xx/5xx / 网络错误 | 标记不可用（EXHAUSTED） |

5 引擎全是幂等 GET，失败后自动换下一 key 重试一次。

## 协议

基于 MCP `2026-07-28` specification，stateless HTTP transport。
健康探活使用 MCP 标准 `ping`（FastMCP 原生支持）。
