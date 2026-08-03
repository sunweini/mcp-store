# Brave MCP — 开发说明

## 概述

Brave Search 的 MCP server，多 key 池（Redis 驱动）自动轮换 + 故障转移，
提供 web_search / local_search 2 个只读搜索工具。

## 架构

```
MCP Client → FastMCP (streamable-http, stateless) → tools/web.py
                                                          │ client_factory
                                                          ▼
                                                     BraveClient (httpx)
                                                          ▲
                                                          │ 取 key / 记账
KeyPool (Redis search:keys:brave + pubsub 热更新) ←───────┘
     │
     ├─ search:keys:channel   ← gateway-admin 编辑 key 后 publish → 热重载
     └─ search:usage:<provider>:<key_id>  ← 用量计数（ZSet 月窗口）
```

- FastMCP v4 + MCP Protocol 2026-07-28（stateless HTTP）
- `tools/web.py`：2 个模块级工具函数（可测试），`register()` 注册 MCP 包装
- `telemetry.py`：OTel traces + Prometheus metrics（`search_*` 指标族）
- `key_pool.py` / `brave_client.py`：复制自 tavily-mcp（KeyPool 逻辑
  provider 无关，原样保留）

## 工具与错误语义

| Tool | 重试策略 | 超时 | 说明 |
|---|---|---|---|
| `brave_web_search` | 换 key 重试 1 次 | 5s | Web 搜索（query/count/offset） |
| `brave_local_search` | 换 key 重试 1 次 | 5s | 本地商家/地点搜索 |

两个工具都是幂等 GET 查询，失败后换下一 key 重试一次。

错误 → KeyPool 记账（`classify_error`）：

| HTTP / 异常 | ErrorKind | 效果 |
|---|---|---|
| 401 | INVALID | 永久剔除 |
| 429 | RATE_LIMIT | 冷却 30s（Retry-After 头未解析，恒用默认——设计选择：避免外部控制 cooldown 过长） |
| 422 + body 含 "subscription token is invalid" | INVALID | 永久剔除（见下） |
| 其余 4xx/5xx | EXHAUSTED | 标记不可用（Brave 无欠费 body 语义，全部归此） |
| 网络/超时 | EXHAUSTED | 同上 |

> **实测注意**：Brave 对无效 subscription token 返回 **422** 而非 401
> （错误 body `detail: "The provided subscription token is invalid."`，
> 2026-08-03 实测）。`classify_error` 对该 422 按 body 文本精确匹配 →
> INVALID（评审 I-1 裁决：**只匹配文本不匹配裸码**——422 还有参数错误
> 语义，裸码匹配会误剔有效 key；其余 422 不映射，落 EXHAUSTED）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `REDIS_URL` | 无（必填） | Redis 连接（key 池存储 + 热更新 pubsub） |
| `MCP_HOST` | `127.0.0.1` | 监听地址 |
| `MCP_PORT` | `9051` | MCP 端口（仓库规范登记，见根 CLAUDE.md） |
| `LOG_FORMAT` | `console` | `console`（开发）/ `json`（生产） |
| `BRAVE_QUOTA_DEFAULT` | `2000` | 未设 monthly_quota 时默认月配额 |
| `PROMETHEUS_PORT` | `9464` | Prometheus /metrics 端口（与 zabbix/tavily
  同机部署会冲突，Task 7 端口迁移时定） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 无 | OTLP collector URL（未设则 console span） |
| `OTEL_SERVICE_NAME` | `brave-mcp` | 服务名（trace/metrics label） |

## KeyPool 设计说明

- key 存 Redis hash `search:keys:brave`，field = key_id（URL-safe 随机
  id，不与 key 本身相关），value = 记录 JSON
- 多实例共享同一池；任一实例写回状态，其他实例 reload 时看到
- 热更新：gateway-admin 改动 key 后 publish 到 `search:keys:channel`，
  server 监听触发 reload。**server.py 必须在 `_start_pool_listener` 里
  `await pool._pubsub.subscribe("search:keys:channel")`**——漏掉则热更新
  静默失效（Task 2 已补，回归风险点）
- key 挑选优先级：健康 > 低配额（<5% 兜底）> 剩余最多

## 可观测性

### Traces
- FastMCP 自动为每个 MCP 操作创建 span；BraveClient 为每次 API 调用
  创建 span（`brave_client.{web.search|local.search}`），非 2xx 标记 ERROR

### Metrics（Prometheus, http://localhost:9464/metrics）
- `search_requests_total{provider, engine, status}` — status 低基数
  （success/error）
- `search_request_duration_seconds` — histogram，bucket 对齐 SLO：
  0.1/0.5/1/3/5
- `search_quota_remaining{provider}` — 池内最低 remaining
- `search_quota_ratio{provider, level}` — 按 provider 聚合的档位
  （warning<10% / critical<5% / exhausted=0，配合 alertmanager 告警）
- `search_key_pool_size{provider}` / `search_key_invalid_total{provider}`
- 接线：配额类指标由 KeyPool 在 `reload()`（启动/热更新）与
  `on_error(INVALID)`（key 剔除）后经 `record_quota_metrics()` 刷新，
  数据源是 `health_snapshot()`（remaining 为本地估算，无官方端点）
- **key_id 与明文 key 禁止进入 metric label 与日志**（OBS-CORE-003）

### Logs
- structlog 结构化 key=value；`LOG_FORMAT=json` 切 JSON
- 所有日志带 `service="brave-mcp"`；错误带 `error` key

## 本地开发

```bash
uv sync --all-extras   # 注意：必须 --all-extras，否则 venv 无 pytest
redis-server --daemonize yes   # 需要本地 Redis 跑冒烟
uv run python server.py        # REDIS_URL 必填
uv run pytest tests/ -v        # 全量测试（KeyPool + BraveClient + 工具层）
```

## 已知注意事项

- `tools/web.py` 的 register 用**显式具名包装**（非 *args 泛型）：
  FastMCP v4.0.0b1 的 ParsedFunction 校验拒绝 *args 工具函数
- `telemetry.py` 的 histogram 参数名是 `explicit_bucket_boundaries_advisory`
  （OTel SDK >= 1.44 移除了旧名 `explicit_bucket_boundaries`）
- 工具函数带 `client_factory` 注入参数（测试用），MCP 包装层不传，
  不出现在 tool schema
- `_call_with_pool` 里 client 关闭方法名是 `close()` 而非 `aclose()`
  （httpx.AsyncClient 才有 aclose；写错则 getattr 恒为 None，连接泄漏
  无感知——tavily 踩过的坑，此处保持已修版本）

## 知识库

开发时查阅 `../knowledge-base/fastmcp-v4/` — FastMCP v4 完整文档。
