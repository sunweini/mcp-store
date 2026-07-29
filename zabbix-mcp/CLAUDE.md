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
| `MCP_HOST` | `127.0.0.1` | 监听地址 |
| `MCP_PORT` | `8000` | 监听端口 |

## 知识库

开发时查阅 `../knowledge-base/fastmcp-v4/` — FastMCP v4 完整文档。
