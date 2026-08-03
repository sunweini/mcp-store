# SerpAPI MCP — 开发说明

## 概述

SerpAPI 的 MCP server，多 key 池（Redis 驱动）自动轮换 + 故障转移，
提供 google / bing / baidu / duckduckgo / ebay 5 个引擎的只读搜索工具。

## 架构

```
MCP Client → FastMCP (streamable-http, stateless) → tools/search.py
                                                          │ client_factory
                                                          ▼
                                                    SerpapiClient (httpx)
                                                          ▲
                                                          │ 取 key / 记账
KeyPool (Redis search:keys:serpapi + pubsub 热更新) ←──────┘
     │
     ├─ search:keys:channel   ← gateway-admin 编辑 key 后 publish → 热重载
     └─ search:usage:<provider>:<key_id>  ← 用量计数（ZSet 月窗口）
```

- FastMCP v4 + MCP Protocol 2026-07-28（stateless HTTP）
- `tools/search.py`：5 个模块级工具函数（可测试），`register()` 注册 MCP 包装
- `telemetry.py`：OTel traces + Prometheus metrics（`search_*` 指标族）
- `key_pool.py` / `serpapi_client.py`：复制自 tavily-mcp（KeyPool 逻辑
  provider 无关，原样保留）

## 工具与错误语义

| Tool | 引擎 | 重试策略 | 超时 | 说明 |
|---|---|---|---|---|
| `serpapi_google` | google | 换 key 重试 1 次 | 5s | Google Web 搜索（gl/hl/num/start） |
| `serpapi_bing` | bing | 换 key 重试 1 次 | 5s | Bing 搜索（gl/hl/cc/count） |
| `serpapi_baidu` | baidu | 换 key 重试 1 次 | 5s | 百度搜索（cti/page_num） |
| `serpapi_duckduckgo` | duckduckgo | 换 key 重试 1 次 | 5s | DuckDuckGo（kl） |
| `serpapi_ebay` | ebay | 换 key 重试 1 次 | 5s | eBay 商品搜索（_nkw/ebay_domain） |

5 引擎全是幂等 GET 查询，失败后换下一 key 重试一次（serpapi 无长任务）。

错误 → KeyPool 记账（`classify_error` 三参数版——tavily/brave 是两参数）：

| HTTP / 响应 | ErrorKind | 效果 |
|---|---|---|
| 401 | INVALID | 永久剔除 |
| 429 | RATE_LIMIT | 冷却 30s（Retry-After 可覆盖） |
| 200 + body 含 quota 关键词 | EXHAUSTED | 永久剔除（欠费） |
| 其余 4xx/5xx | EXHAUSTED | 标记不可用 |
| 网络/超时 | EXHAUSTED | 同上 |

> **EXHAUSTED 判据**（serpapi 特有）：欠费返回 **200** + error body 而非
> 4xx（实测 body：`"Account has exceeded quota, for more info visit
> https://serpapi.com/pricing"`）。关键词（大小写不敏感）：
> `account has exceeded quota` / `quota exceeded` / `insufficient credits`。
> 工具层调用 classify_error 时必须传 `resp.text`（SerpapiError.detail
> 存截断 body ≤200 字符，关键词在 body 开头，截断不影响判据）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `REDIS_URL` | 无（必填） | Redis 连接（key 池存储 + 热更新 pubsub） |
| `MCP_HOST` | `127.0.0.1` | 监听地址 |
| `MCP_PORT` | `9052` | MCP 端口（仓库规范登记，见根 CLAUDE.md） |
| `LOG_FORMAT` | `console` | `console`（开发）/ `json`（生产） |
| `SERPAPI_QUOTA_DEFAULT` | `100` | 未设 monthly_quota 时默认月配额 |
| `PROMETHEUS_PORT` | `9464` | Prometheus /metrics 端口（与 zabbix/tavily/
  brave 同机部署会冲突，Task 7 端口迁移时定） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 无 | OTLP collector URL（未设则 console span） |
| `OTEL_SERVICE_NAME` | `serpapi-mcp` | 服务名（trace/metrics label） |

## KeyPool 设计说明

- key 存 Redis hash `search:keys:serpapi`，field = key_id（URL-safe 随机
  id，不与 key 本身相关），value = 记录 JSON
- 多实例共享同一池；任一实例写回状态，其他实例 reload 时看到
- 热更新：gateway-admin 改动 key 后 publish 到 `search:keys:channel`，
  server 监听触发 reload。**server.py 必须在 `_start_pool_listener` 里
  `await pool._pubsub.subscribe("search:keys:channel")`**——漏掉则热更新
  静默失效
- key 挑选优先级：健康 > 低配额（<5% 兜底）> 剩余最多

## 可观测性

### Traces
- FastMCP 自动为每个 MCP 操作创建 span；SerpapiClient 为每次 API 调用
  创建 span（`serpapi_client.{engine}`），非 2xx / 业务错误标记 ERROR

### Metrics（Prometheus, http://localhost:9464/metrics）
- `search_requests_total{provider, engine, status}` — status 低基数
  （success/error）
- `search_request_duration_seconds` — histogram，bucket 对齐 SLO：
  0.1/0.5/1/3/5
- `search_quota_remaining{provider}` / `search_quota_ratio{provider}`
  / `search_key_pool_size{provider}` / `search_key_invalid_total{provider}`
  — 由 server 层周期性采集（Task 3+ 接线）
- **key_id 与明文 key 禁止进入 metric label 与日志**（OBS-CORE-003）

### Logs
- structlog 结构化 key=value；`LOG_FORMAT=json` 切 JSON
- 所有日志带 `service="serpapi-mcp"`；错误带 `error` key
- **明文 key 泄漏防线**（serpapi 特有，tavily/brave 无此问题）：
  - api_key 是 URL query 参数——httpx 默认在 INFO 级打印带 query 的
    完整请求 URL。`logging_config.py` 必须把 httpx logger 提到 WARNING
    （`logging.getLogger("httpx").setLevel(logging.WARNING)`），测试
    `tests/test_logging.py` 是回归防线
  - `SerpapiClient.search` 的 httpx 网络异常只记 `type(exc).__name__`，
    不记 `str(exc)`（httpx 异常 repr 含完整 URL）；span 的 http.url
    只记 path 不记 query

## 本地开发

```bash
uv sync --all-extras   # 注意：必须 --all-extras，否则 venv 无 pytest
redis-server --daemonize yes   # 需要本地 Redis 跑冒烟
uv run python server.py        # REDIS_URL 必填
uv run pytest tests/ -v        # 全量测试（KeyPool + SerpapiClient + 工具层）
```

## 已知注意事项

- `tools/search.py` 的 register 用**显式具名包装**（非 *args 泛型）：
  FastMCP v4.0.0b1 的 ParsedFunction 校验拒绝 *args 工具函数
- `telemetry.py` 的 histogram 参数名是 `explicit_bucket_boundaries_advisory`
  （OTel SDK >= 1.44 移除了旧名 `explicit_bucket_boundaries`）
- 工具函数带 `client_factory` 注入参数（测试用），MCP 包装层不传，
  不出现在 tool schema
- `_call_with_pool` 里 client 关闭方法名是 `close()` 而非 `aclose()`
  （httpx.AsyncClient 才有 aclose；写错则 getattr 恒为 None，连接泄漏
  无感知——tavily 踩过的坑，此处保持已修版本）
- `SerpapiClient.search` 用 `dict(params)` 拷贝后再追加 engine/api_key，
  不污染调用方 dict（brief 明示）
- key_pool.py 的 `get_message()` 不传 `ignore_subscribe=True`（redis-py
  ≥6 改名，必 TypeError 静默失效）——tavily/brave 踩过，此处保持已修版

## 知识库

开发时查阅 `../knowledge-base/fastmcp-v4/` — FastMCP v4 完整文档。
