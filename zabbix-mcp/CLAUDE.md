# Zabbix MCP — 开发说明

## 概述

Zabbix 监控系统的 MCP server，提供告警巡检、维护期管理、告警确认能力。

## 架构

- FastMCP v4 + MCP Protocol 2026-07-28 (stateless HTTP)
- ZabbixClient: httpx async + API Token 认证
- 可观测性: structlog + OpenTelemetry
- 9 个 tool: problems(2) + maintenance(3) + events(3) + summary(1)

## 安全模型

- 读操作 (readOnlyHint=True): 自动执行
- 写操作 (destructiveHint=True): docstring 标注 ⚠️ 写操作，AI 需用户确认

## 本地开发

```bash
uv sync
uv run python server.py   # 需设置 ZABBIX_URL + ZABBIX_TOKEN
uv run pytest tests/ -v
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `ZABBIX_URL` | 无（必填） | Zabbix API URL |
| `ZABBIX_TOKEN` | 无（必填） | API Token |
| `ZABBIX_TIMEOUT` | `30` | HTTP 超时秒数 |
| `MCP_HOST` | `127.0.0.1` | MCP server 监听地址 |
| `MCP_PORT` | `8000` | MCP server 监听端口 |
| `LOG_FORMAT` | `console` | `console`（开发）/ `json`（生产） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 无 | OTLP collector URL（如 `http://localhost:4317`） |
| `OTEL_SERVICE_NAME` | `zabbix-mcp` | 服务名（trace/metrics label） |
| `PROMETHEUS_PORT` | `9464` | Prometheus /metrics 端口 |
| `FASTMCP_TELEMETRY_MODE` | `native` | `native` / `propagation_only` / `off` |

## 可观测性

### Traces
- FastMCP 自动为每个 MCP 操作创建 span（`tools/call {name}` 等）
- ZabbixClient 为每次 Zabbix API 调用创建 span（`zabbix_client.{method}`）
- 设 `OTEL_EXPORTER_OTLP_ENDPOINT` 导出到 Jaeger/Tempo/OTLP collector
- 不设则 console 输出（开发用）

### Metrics（Prometheus）
- `http://localhost:9464/metrics` 暴露 7 个核心指标
- `zabbix_mcp_requests_total` — tool 调用总数
- `zabbix_mcp_request_duration_seconds` — tool 调用延迟
- `zabbix_mcp_errors_total` — tool 错误数
- `zabbix_mcp_dependency_duration_seconds` — Zabbix API 延迟
- `zabbix_mcp_dependency_errors_total` — Zabbix API 错误数
- `zabbix_mcp_in_flight_requests` — 处理中请求数

### Logs
- structlog 结构化日志（key=value）
- 每条日志自动注入 `trace_id` + `span_id`
- `LOG_FORMAT=json` 切换 JSON 输出（Loki/ELK 友好）

## 知识库

开发时查阅 `../knowledge-base/fastmcp-v4/` — FastMCP v4 完整文档。
