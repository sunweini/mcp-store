# tavily-mcp

Tavily 搜索 MCP server：多 key 池（Redis 驱动）自动轮换与故障转移，
为 AI agent 提供 web 搜索、网页正文提取、深度研究等能力。全部工具只读。

## 功能

| Tool | 类型 | 说明 |
|---|---|---|
| `tavily_search` | 读 | Web 搜索（basic/advanced，支持 news/finance 主题、AI 摘要） |
| `tavily_extract` | 读 | 从 URL 提取干净正文（1-10 个 URL） |
| `tavily_crawl` | 读 | 网站爬取，结构化数据（长任务，不自动重试） |
| `tavily_map` | 读 | Map 搜索：一次查询返回多主题 URL 列表 |
| `tavily_research` | 读 | 深度研究：多源收集 + AI 回答（长任务，不自动重试） |

## 快速开始

### 连接配置

```json
{
  "mcpServers": {
    "tavily": {
      "url": "http://localhost:9050/mcp"
    }
  }
}
```

### 环境变量

```bash
export REDIS_URL="redis://localhost:6379/0"   # 必填
```

### 从源码运行

```bash
git clone <repo>
cd tavily-mcp
uv sync --all-extras
export REDIS_URL="redis://localhost:6379/0"
uv run python server.py
```

### 运行测试

```bash
cd tavily-mcp && uv run pytest tests/ -v
```

### Docker

```bash
docker run -p 9050:9050 -e REDIS_URL=redis://host.docker.internal:6379/0 \
  -e MCP_HOST=0.0.0.0 -e MCP_PORT=9050 <image>
```

## API Key 管理

Key 池存放在 Redis `search:keys:tavily`，由 **gateway-admin 的 API Keys
页面**统一管理（添加 / 启用停用 / 查看配额）。管理员改动后 server 通过
pubsub 热更新自动生效，无需重启。

Key 记录字段：

| 字段 | 说明 |
|---|---|
| `key` | Tavily API key |
| `monthly_quota` | 月配额（未设则用 `TAVILY_QUOTA_DEFAULT`） |
| `status` | `active` / `invalid` / `exhausted` / `cooldown` / `low_quota` |
| `remaining` | 剩余配额（错误或 usage 校验时更新） |
| `enabled` | 手动停用开关 |

## 协议

基于 MCP `2026-07-28` specification，stateless HTTP transport。
健康探活使用 MCP 标准 `ping`（FastMCP 原生支持）。
