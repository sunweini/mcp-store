# Multi-Search MCP 整合设计

日期：2026-08-03
状态：待审阅
相关文档：`2026-07-30-mcp-gateway-design.md`（gateway 架构）

## 背景

现有 3 个搜索源各有独立官方 MCP：tavily（search/extract/crawl/map/research）、brave-search（web/local search）、serpapi（google/bing/baidu/duckduckgo/ebay 5 引擎）。每个源有独立 API key、独立免费额度（tavily 月 1000 次、brave 月 2000 次、serpapi 月 100 次）。

目标：把 3 个源整合进 MCP Gateway 体系，支持**每源配置多个 API key**，实现负载均衡（同源多 key 轮换）与欠费自动切换（失效 key 自动剔除、请求降级到可用 key），并提供**前台管理**（gateway-admin 页面管理 key 池）。

本次只新增搜索 MCP 相关组件，不动 gateway-proxy；对 gateway-admin 仅新增 API Keys 管理模块。

## 架构决策（已确认）

- **方案 B：三源独立 server，无聚合层**。tavily-mcp / brave-mcp / serpapi-mcp 三个独立 FastMCP server，各管各的 key 池，全部注册进 gateway。不设统一 web_search 聚合入口——AI 直接按源选择工具。
- **key 存储：Redis**（复用 gateway 现有实例）。按源分组存储，key 变更通过 Redis Pub/Sub 热更新，进程不重启。
- **前台：gateway-admin 加 API Keys 模块**。按源分组的 CRUD + 状态展示 + 用量看板。
- **切换策略：同源多 key 轮换 + 失效/欠费自动剔除**。无跨源优先级链（无聚合层，该需求取消）。
- **用量统计：本地计数 + 官方用量接口**（tavily 有 `/usage`；brave/serpapi 无公开接口，本地计数为主）。
- **部署：3 个新 server 以 HTTP Streamable 常驻，接入 gateway**（与 zabbix-mcp 一致）。
- **KeyPool 实现共享：复制三份**，遵守"每 MCP 独立目录独立发布"约定。KeyPool 为纯逻辑，每源因 API 错误语义差异会有小改，三处复制可接受。

## 架构

```
MCP Client → gateway-proxy:8082
                  ├→ tavily-mcp   (容器内 :9050)  tavily_search / extract / crawl / map / research
                  ├→ brave-mcp    (容器内 :9051)  brave_web_search / brave_local_search
                  └→ serpapi-mcp  (容器内 :9052)  serpapi_google / bing / baidu / duckduckgo / ebay
                        ↑ 注册/探活/转发（proxy 经容器名+容器内端口互访）
        gateway-admin:8081 (新增 API Keys 管理模块)
                        ↑ 读写
                    Redis (key 池 + 健康状态 + 用量)
```

端口规范：**MCP server 容器内端口统一 9050-9500**（仓库约定，见根 CLAUDE.md）。分配：tavily-mcp=9050、brave-mcp=9051、serpapi-mcp=9052、zabbix-mcp=9053（本次一并迁移）。均**不映射宿主端口**——与 gateway-proxy/admin 的宿主端口（8082/8081）互不冲突，减少攻击面。

### 组件清单

| 组件 | 内容 |
|---|---|
| `tavily-mcp/` | server.py + KeyPool + TavilyClient，5 tools |
| `brave-mcp/` | server.py + KeyPool + BraveClient，2 tools |
| `serpapi-mcp/` | server.py + KeyPool + SerpapiClient，5 engines |
| gateway-admin | `api/keys.py` + 前端 API Keys 页 + Redis schema |
| deploy/ | 3 新 server 入 docker-compose + init.sh 注册 |

## Redis Schema

```
search:keys:<provider>                  Hash — key 池
  <key_id> → JSON {
    "key": "tvly-...",            # 明文存储（内网，与 gateway 既有做法一致）
    "provider": "tavily",
    "enabled": true,
    "monthly_quota": 1000,        # 月配额上限，可改
    "status": "active|low_quota_warning|low_quota|invalid|exhausted|cooldown",
    "cooldown_until": null | ISO8601,
    "remaining": null | int,      # 官方剩余配额（tavily 有；brave/serpapi 靠本地计数）
    "last_used_at": null | ISO8601,
    "last_error": null | str,
    "created_at": ISO8601
  }

search:usage:<provider>:<key_id>        ZSet — 本地用量计数
  member=epoch_ms, score=epoch_ms        （按月窗口滚动统计）

search:keys:channel                     Pub/Sub 频道 — key 变更通知
  消息: {"provider": "tavily", "action": "upsert|delete", "key_id": "..."}
```

`<provider>` 取值：`tavily` / `brave` / `serpapi`。

## KeyPool 设计（每源复制一份，逻辑相同）

```python
class KeyPool:
    """单源 key 池。Redis 加载 + 轮换 + 失效剔除 + 热更新。"""

    def __init__(self, provider: str, redis, pubsub, quota_default: int): ...

    async def next_key(self) -> KeyRecord | None:
        """轮询选中:
        1. enabled=True 且 status != invalid
        2. cooldown 未过期的跳过
        3. low_quota（剩余<5%）跳过，仅当池内其余全不可用时兜底
        4. 多 key 时优先 remaining 高者（配额感知），tie 按配置顺序
        5. 池空/全不可用 → None
        """

    async def on_success(self, key_id: str, remaining: int | None = None):
        """记成功: status=active, cooldown 清零, remaining 更新, 写回 Redis + 计数"""

    async def on_error(self, key_id: str, kind: ErrorKind):
        """记失败:
        - INVALID (401/403): status=invalid, 永久剔除
        - EXHAUSTED (配额 0 / 欠费响应): status=exhausted, 永久剔除, remaining=0
        - RATE_LIMIT (429): status=cooldown, cooldown_until=now+Retry-After(默认 30s)
        """

    async def reload(self):
        """订阅 search:keys:channel，重读本源 key 组 → 热更新（增删/启停/改配额）"""

    async def health_snapshot(self) -> list[dict]:
        """池状态摘要，写回 Redis 供前台展示（按需）"""
```

### 错误语义映射（各源不同）

| 源 | 失效/欠费 | 限流 |
|---|---|---|
| tavily | 401/403 → INVALID | 429 → RATE_LIMIT |
| brave | 401 → INVALID | 429 → RATE_LIMIT |
| serpapi | 响应体含 account limit 类错误 → EXHAUSTED；401 → INVALID | 429 → RATE_LIMIT |

429 带 `Retry-After` 头则用之，否则默认 30s。

### 低配额阈值（自动切换 + 告警）

- **可用量 < 5%**：key 状态标 `low_quota`。KeyPool 正常轮询时**跳过**该 key；仅当池内其余 key 全不可用（invalid/cooldown/exhausted）时**兜底使用**，避免浪费剩余配额。
- **可用量 < 10%**：状态标 `low_quota_warning`（仍正常参与轮询），前台告警提示。
- 可用量计算：`remaining / monthly_quota`；`remaining` 无官方数据时（brave/serpapi）用 `monthly_quota - 本地当月计数`。`monthly_quota` 未设置或 `remaining` 未知时，该 key 不触发低配额阈值（视为正常参与轮询），前台显示"—"。

## 各源工具

### tavily-mcp（5 tools）

| 工具 | 参数（要点） |
|---|---|
| `tavily_search` | query*, search_depth, topic, days, max_results(默认5), include_answer, include_raw_content, include_images |
| `tavily_extract` | urls*（支持多个）, extract_depth |
| `tavily_crawl` | urls*, max_depth, max_pages, max_cost |
| `tavily_map` | query*, search_depth, max_results(默认100) |
| `tavily_research` | query*, max_depth, max_learnings, max_sources, max_browser_pages |

- 全部标 `readOnlyHint=True`，docstring 写清用途。
- 请求 `POST https://api.tavily.com/search|extract|crawl|map|research`，`Authorization: Bearer <key>`。
- 用量：`GET /usage` 拿官方 remaining 写回 Redis（每次 search 后或定时）。

### brave-mcp（2 tools）

| 工具 | 参数（要点） |
|---|---|
| `brave_web_search` | query*(≤400字符/50词), count(1-20, 默认10), offset(0-9) |
| `brave_local_search` | query*, count(1-20, 默认5) |

- 请求 `GET https://api.search.brave.com/res/v1/web/search|local/search`，`X-Subscription-Token: <key>`。
- 响应含 `web.results` / `local.results`，返回 `web.results` 即可（字段含 title/url/description）。

### serpapi-mcp（5 engines）

| 工具 | 参数（要点） |
|---|---|
| `serpapi_google` | query*, 可选 gl/hl/num/start 等 |
| `serpapi_bing` | query*, 可选 gl/hl/cc/count |
| `serpapi_baidu` | query*, 可选 cti/page_num |
| `serpapi_duckduckgo` | query*, 可选 kl |
| `serpapi_ebay` | _nkw*, 可选 ebay_domain |

- 请求 `GET https://serpapi.com/search.json?engine=<engine>&...`，`api_key` 参数。
- 返回解析出 `organic_results`（google/bing/baidu/ddg）或 `organic_results`/`shopping_results`（ebay）摘要。
- 与官方 `mcp-serpapi` 的区别：多 key 池 + 结构化错误 + 用量计数 + FastMCP。

## 错误处理

- **池空/全不可用**：工具返回明确错误 `该源所有 API key 不可用: #1 invalid #2 cooldown 至 14:32`。
- **单 key 失败**：自动换池内下一可用 key 重试 1 次。**仅幂等轻查询自动重试**（tavily_search/map/extract、brave 两工具、serpapi 各引擎）；**tavily_crawl/research 为长任务不重试**，直接返回错误（避免重复消耗配额与时间）。再次失败返回错误。
- **外部 API 超时**：httpx 5s 超时，超时返回错误不重试（避免浪费配额）。crawl/research 超时上限放宽至 60s。
- **Redis 不可用**：key 池退化为启动时加载的静态快照，请求照常（容忍 Redis 短暂故障）；写回失败仅记日志。
- **探活失败**：新 key 添加时失败则标 invalid 不入池，前台可见原因。探活计入该 key 配额但不计入本地用量统计。

## 前台管理（gateway-admin API Keys 模块）

### API

```
GET    /api/search-keys/{provider}              列出某源 key 池 + 状态
POST   /api/search-keys/{provider}              添加 key {key, monthly_quota}（自动探活）
PUT    /api/search-keys/{provider}/{key_id}     启停 / 改 monthly_quota
DELETE /api/search-keys/{provider}/{key_id}     删除 key
GET    /api/search-keys/{provider}/usage        用量看板（本地计数 + tavily 官方 remaining）
```

- 需要登录（复用现有 JWT auth）。
- **探活**：添加时用该 key 发一次最小查询（`q="ping"`，max_results=1），成功 → active 写入 remaining；失败 → invalid，前台显示原因，不入池。
- 探活会消耗 1 次配额（serpapi 月 100 次时更明显），添加失败/成功均在 UI 提示本次探活计入配额。
- 写 Redis 后 PUBLISH `search:keys:channel`，源 server 热更新。

### 前端

gateway-admin 现有 Vue3 前端加 "API Keys" 菜单页：
- 按源 tab 分组（tavily / brave / serpapi）
- 表格：key 别名/状态/剩余配额/本月用量/最后使用/错误
- **余额告警（<10%）**：key 行状态标红 + 显示剩余百分比与"低配额"标签；源 tab 显示告警计数角标（如 `tavily 2 低配额`）
- **低配额（<5%）**：key 行标红显示"即将耗尽（兜底模式）"，角标同样计数
- 操作：添加（自动探活）、启停、删除、改配额

## 部署

- 3 个新 server 加入 `deploy/docker-compose.yml`（**容器内端口 9050/9051/9052，不映射宿主端口**）；zabbix-mcp 同步迁移容器内 8000 → 9053。Dockerfile 复用 base。
- `init.sh` 扩展：注册 3 个 server 到 gateway + 探活。
- key 管理全部走前台，容器内不配 key env（源 server 启动时从 Redis 拉取）。

## 可观测性

遵循 `~/.claude/docs/observability-coding-standards.md`：

- 结构化日志（structlog key=value），携带 service/trace_id/request_id/route
- 每请求记录：provider、key_id（**不入 metric label**）、HTTP 状态、耗时、配额 remaining
- Prometheus 指标（每源 server）：
  - `search_requests_total{provider, engine, status}`（低基数：status 分 success/rate_limit/invalid/exhausted/timeout）
  - `search_quota_remaining{provider}`（可告警：配额耗尽 → 提示换 key）
  - `search_quota_ratio{provider, level}`（**按 provider 聚合，取该源最低 remaining 的 key**，避免 key 级高基数 label；level: warning<10% / critical<5% / exhausted=0，配合 alertmanager 告警）
  - `search_key_pool_size{provider}`、`search_key_invalid_total{provider}`
  - histogram `search_request_duration_seconds` 自定义 bucket 对齐 SLO（100ms/500ms/1s/3s/5s）
- key_id / api key 本身**禁止**入 metric label 与日志（敏感，高基数）

## 测试

- **KeyPool 单测**：轮询顺序、配额感知选择、cooldown 过期恢复、invalid/exhausted 剔除、热更新 reload、池空返回 None、**low_quota 跳过与兜底**、**low_quota_warning 仍参与轮询**
- **各源 client 单测**：httpx MockTransport 模拟成功/429/401/超时，验证错误映射与重试
- **探活单测**：最小查询成功/失败
- **工具层单测**：FastMCP tool 调用 → 正确参数透传
- **gateway-admin**：keys API 的 CRUD + 探活 mock + 权限
- 集成测试（可选）：本机起 Redis + 3 server，注册进 gateway，端到端搜索

## 非目标（明确排除）

- 不做统一 web_search 聚合层（无跨源 failover、无多源合并）
- 不动 gateway-proxy
- 不做 key 加密存储（内网 Redis，与 gateway 既有 token 存储一致）
- 不做 crawl/research 的进度回调（Tavily 长任务按同步轮询处理）
