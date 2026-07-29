# {{MCP_NAME}} — 开发说明

## 概述

<!-- 简述这个 MCP 做什么 -->

## 架构

<!-- 关键设计决策、为什么这样实现 -->

## 依赖

- FastMCP v4 (`fastmcp==4.0.0b1`)
- MCP Protocol `2026-07-28`（stateless HTTP）

## 知识库

开发本 MCP 时，遇到 API 不确定必须先查知识库：
- 根目录 `knowledge-base/fastmcp-v4/` — FastMCP v4 完整文档
- 触发规则见根 `CLAUDE.md` 的「强制规则」表
- 常用：`11-tools.md`（tool 定义）、`15-sessions.md`（状态管理）、`40-telemetry.md`（可观测性）

## 本地开发

```bash
# 安装依赖
uv sync

# 启动 server
uv run python server.py

# 运行测试
uv run pytest tests/
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MCP_HOST` | `127.0.0.1` | 监听地址 |
| `MCP_PORT` | `8000` | 监听端口 |

## 注意事项

<!-- 开发中踩过的坑、特殊处理 -->
