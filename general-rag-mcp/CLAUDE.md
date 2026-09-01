# general-rag-mcp — 开发说明

## 概述

general-rag 知识库检索的 MCP server：让任意 agent 通过 MCP 检索
general-rag 知识库（章管家接口文档等），返回带来源标记的答案。
对接契约见 `/Users/sunweini/同步空间/工作内容/AI工作项目/general-rag/docs/mcp-integration-guide.md`。

## 架构

```
MCP Client → FastMCP (streamable-http, stateless) → tools/knowledge_base.py
                                                          │
                                                          ▼
                                                   RagClient (httpx)
                                                          │
                                                          ▼
                                   general-rag REST API (10.33.17.72:8001/api/v1)
```

- FastMCP v4 + MCP Protocol 2026-07-28（stateless HTTP）
- `tools/knowledge_base.py`：4 个模块级工具函数（可测试），`register()` 注册 MCP 包装
- `rag_client.py`：REST client + 超时/重试/snippet 截断
- `telemetry.py`：OTel traces + Prometheus metrics

## 工具与错误语义

| Tool | 类型 | 超时 | 重试 | 说明 |
|---|---|---|---|---|
| `knowledge_base_search` | 读 | 60s | 503 退避 1s/2s/4s ×3 | 核心检索 |
| `knowledge_base_namespaces` | 读 | 60s | 503 退避 | 列出 namespace |
| `knowledge_base_health` | 读 | 60s | 503 退避 | 组件状态 |
| `knowledge_base_ingest` | **写** | 60s | 不重试 | 文件摄入（非幂等） |

错误语义（指南 §4.4 / §7）：

| HTTP | 处理 |
|---|---|
| 400 | 不重试；search 提示先调 namespaces 确认 namespace |
| 503 | 读接口指数退避重试（骨干故障）；ingest 不重试 |
| 500 | 透传错误 |
| 网络/超时 | 读接口退避重试后抛 RagConnectionError；tool 层转"服务暂不可用" |

## 关键设计决策

- **namespace 缺省 `stamp-project`**（非必填）：指南 §8 schema 是 required，
  但生产只有单一 namespace；缺省落 stamp-project 而非 default，规避
  §7.1 的 `ALLOW_DEFAULT_NS=false` 时 400。
- **返回结构化 dict**（非 markdown）：answer + sources[]（snippet 截断
  500 字）+ degraded + missing_components + gap_warning，由宿主自行渲染。
- **snippet 截断**（隐私，指南 §3）：日志与返回体都不完整转储文档内容。
- **ingest 是写操作**：`destructiveHint=True`，docstring 含 `⚠️ 写操作`；
  20MB 上限在 tool 层校验（后端 413 兜底）。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `GENERAL_RAG_BASE_URL` | `http://10.33.17.72:8001/api/v1` | 后端基址 |
| `GENERAL_RAG_TIMEOUT` | `60` | 外部调用超时（秒） |
| `MCP_HOST` | `127.0.0.1` | 监听地址 |
| `MCP_PORT` | `9055` | MCP 端口（仓库登记） |
| `LOG_FORMAT` | `console` | `console` / `json` |
| `PROMETHEUS_PORT` | `9464` | Prometheus /metrics 端口 |

## 可观测性

- **Traces**：FastMCP 自动为 MCP 操作创建 span；RagClient 为每次 API
  调用创建 span（`rag_client.{path}`）。
- **Metrics**（/metrics）：`general_rag_mcp_requests_total` /
  `request_duration_seconds` / `errors_total` / `dependency_duration_seconds`
  / `dependency_errors_total` / `in_flight_requests`。label 只含 tool_name
  与 error_type（低基数）；文档内容/query 明文禁止进 label（OBS-CORE-003）。
- **Logs**：structlog 结构化 key=value；带 `service="general-rag-mcp"`；
  错误带 `error` key；日志不 dump 响应体（可能含文档内容）。

## 本地开发

```bash
uv sync --all-extras
uv run python server.py
uv run python -m pytest tests/ -q
```

## 已知注意事项

- `tools/knowledge_base.py` 的 register 用**显式具名包装**（非 *args 泛型）：
  FastMCP v4.0.0b1 的 ParsedFunction 校验拒绝 *args 工具函数。
- 工具函数带 `client` 注入参数（测试用），MCP 包装层不传，不出现在
  tool schema。

## 知识库

开发时查阅 `../knowledge-base/fastmcp-v4/` — FastMCP v4 完整文档。
