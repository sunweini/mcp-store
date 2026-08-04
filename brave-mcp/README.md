# brave-mcp

Brave Search MCP server：多 key 池（Redis 驱动）自动轮换与故障转移，
为 AI agent 提供 web 搜索与本地商家搜索能力。全部工具只读。

## 功能

| Tool | 类型 | 说明 |
|---|---|---|
| `brave_web_search` | 读 | Web 搜索（query/count/offset，结果含 title/url/description） |
| `brave_local_search` | 读 | 本地商家/地点搜索 |

## 快速开始

### 连接配置

```json
{
  "mcpServers": {
    "brave": {
      "url": "http://localhost:9051/mcp"
    }
  }
}
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `REDIS_URL` | 无（必填） | Redis 连接（key 池存储 + 热更新 pubsub） |
| `MCP_HOST` | `127.0.0.1` | 监听地址 |
| `MCP_PORT` | `9051` | MCP 端口 |
| `LOG_FORMAT` | `console` | `console`（开发）/ `json`（生产） |
| `BRAVE_QUOTA_DEFAULT` | `2000` | 未设 monthly_quota 时默认月配额 |
| `PROMETHEUS_PORT` | `9464` | Prometheus /metrics 端口（与 zabbix/tavily 同机部署会冲突，部署时需错开） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 无 | OTLP collector URL（未设则 console span） |
| `OTEL_SERVICE_NAME` | `brave-mcp` | 服务名（trace/metrics label） |
| `SEARCH_PROXY` | 空（直连） | HTTP 代理 URL（如 `http://10.16.12.12:7890`）。生产网络 api.search.brave.com 直连不通（IPv4 被墙/IPv6 不通），仅 brave 需要走代理；tavily/serpapi 直连通，不要配此变量 |

### 从源码运行

```bash
git clone <repo>
cd brave-mcp
uv sync --all-extras
export REDIS_URL="redis://localhost:6379/0"
uv run python server.py
```

### 运行测试

```bash
cd brave-mcp && uv run pytest tests/ -v
```

### Docker

```bash
docker run -p 9051:9051 -e REDIS_URL=redis://host.docker.internal:6379/0 \
  -e MCP_HOST=0.0.0.0 -e MCP_PORT=9051 <image>
```

## API Key 管理

Key 池存放在 Redis `search:keys:brave`，由 **gateway-admin 的 API Keys
页面**统一管理（添加 / 启用停用 / 查看配额）。管理员改动后 server 通过
pubsub 热更新自动生效，无需重启。

Key 记录字段：

| 字段 | 说明 |
|---|---|
| `key` | Brave Search API key（`X-Subscription-Token` header） |
| `monthly_quota` | 月配额（未设则用 `BRAVE_QUOTA_DEFAULT`） |
| `status` | `active` / `invalid` / `exhausted` / `cooldown` / `low_quota` |
| `remaining` | 剩余配额（错误或用量校验时更新） |
| `enabled` | 手动停用开关 |

## 协议

基于 MCP `2026-07-28` specification，stateless HTTP transport。
健康探活使用 MCP 标准 `ping`（FastMCP 原生支持）。
