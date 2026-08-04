# MCP Store — 多 MCP 开发仓库

基于 [FastMCP v4](https://fastmcp.dev) 的 MCP 服务集合 + 网关管理平台。每个 MCP 独立目录、独立依赖、独立发布。

## 架构

```
MCP Client → gateway-proxy:8082 ──→ zabbix-mcp:9053 (告警巡检)
      ↑              │        ├──→ tavily-mcp:9050  (搜索 5 tools)
      │              │        ├──→ brave-mcp:9051   (搜索 2 tools)
      │              │        └──→ serpapi-mcp:9052 (搜索 5 engines)
      │              ↓
gateway-admin:8081 (Server/Token/API Keys 管理 + 监控面板)
      │
   Redis (共享存储: server 注册 / token / key 池 / 审计)
```

- **gateway-proxy**：MCP 协议代理，Token 认证，读写权限控制，失败审计
- **gateway-admin**：管理 API + Vue 3 前端（Server 管理、Token 管理、API Keys 管理、监控面板）
- **搜索 MCP**：多 API key 池（Redis 驱动），轮换 + 欠费剔除 + 低配额告警（<10% 前台提示 / <5% 兜底切换）

## 组件

| 目录 | 说明 | 端口（容器内） |
|---|---|---|
| `gateway-proxy/` | MCP 网关代理 | 宿主 8082 |
| `gateway-admin/` | 管理 API + Vue 3 前端 | 宿主 8081 |
| `zabbix-mcp/` | Zabbix 告警巡检（8 tools） | 9053 |
| `tavily-mcp/` | Tavily 搜索（search/extract/crawl/map/research） | 9050 |
| `brave-mcp/` | Brave 搜索（web/local） | 9051 |
| `serpapi-mcp/` | SerpAPI 搜索（google/bing/baidu/duckduckgo/ebay） | 9052 |
| `deploy/` | Docker Compose 一键部署 | — |
| `knowledge-base/` | FastMCP v4 官方文档 | — |
| `docs/superpowers/` | 设计 spec + 实施计划 | — |

## 快速开始

### 本地开发

```bash
# 1. 起 Redis
redis-server --daemonize yes

# 2. 起 gateway-admin（管理界面 :8081）
cd gateway-admin && uv sync --all-extras
REDIS_URL=redis://localhost:6379/0 JWT_SECRET=dev uv run uvicorn app:app --port 8081

# 3. 起 gateway-proxy（MCP 入口 :8082）
cd gateway-proxy && uv sync
REDIS_URL=redis://localhost:6379/0 uv run python server.py

# 4. 起搜索 MCP（例：tavily）
cd tavily-mcp && uv sync --all-extras
REDIS_URL=redis://localhost:6379/0 uv run python server.py
```

### 正式环境部署

```bash
cd deploy
cp config/*.env.example config/*.env   # 编辑真实值
bash deploy.sh                          # 构建 + 启动全部容器
bash init.sh                            # 注册 server + 创建 token
```

访问 `http://<host>:8081` 管理界面：注册 server、创建 token、**API Keys 页添加搜索 key**（自动探活验证）。

## 搜索 MCP 多 Key 池

- key 存 Redis `search:keys:<provider>`，前台 API Keys 页管理（增删/启停/改配额）
- 同源多 key 轮换，配额感知（优先剩余多者）
- 401/403 → 失效剔除；429 → 冷却；配额耗尽 → 欠费剔除
- 剩余 <10% 前台标红提示；<5% 跳过正常轮询（仅兜底用）
- 添加 key 自动探活（消耗 1 次配额）

## 配置

| 环境变量 | 说明 |
|---|---|
| `REDIS_URL` | Redis 连接（proxy/admin/MCP 共用） |
| `JWT_SECRET` | admin 登录签名密钥 |
| `SEARCH_PROXY` | brave 出网代理（生产网络直连不通时必配） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTel trace 导出（可选） |

## 开发约定

- FastMCP v4（`fastmcp==4.0.0b1`）+ MCP Protocol `2026-07-28`，streamable-http stateless
- uv 包管理（`--prerelease=allow`）
- MCP 容器内端口统一 9050-9500，不映射宿主
- 工具标注 annotations 读写分离；写操作 docstring 含 `⚠️ 写操作`
- 结构化日志 + OpenTelemetry（详见 `~/.claude/docs/observability-coding-standards.md`）
- 写代码前先读 `knowledge-base/fastmcp-v4/` 对应文档

详细规范见 [CLAUDE.md](CLAUDE.md)。
