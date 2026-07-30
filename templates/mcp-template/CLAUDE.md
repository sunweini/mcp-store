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

## Gateway 接入

### 0. Server 描述与命名（必须）

- **Server name**：小写字母/数字/连字符，**禁止下划线**（namespace 前缀用 `_` 切分，含下划线会路由歧义）。如 `zabbix`、`github`、`my-db`。
- **Server 描述**：一句话说清这个 MCP 做什么，注册时填入，展示在管理界面。
- **Tool 描述**：每个 tool 的 docstring 写清楚用途，Gateway 会拉取展示给管理员配权限时参考。写操作 docstring 必须含 `⚠️ 写操作` 标记。

```python
mcp = FastMCP(
    "{{mcp-name}}",
    instructions="一句话说清这个 MCP 的能力和使用方式。",
)
```

### 1. Tool 读写分离（必须）

所有 tool 必须标注 annotations，Gateway 依赖此进行权限控制：

```python
from mcp.types import ToolAnnotations

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def read_operation(...):
    """读操作 — 查询、列表、搜索等不修改数据的操作。"""
    pass

@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
def write_operation(...):
    """写操作 — 创建、修改、删除等修改数据的操作。
    ⚠️ 写操作 — 执行前必须向用户确认参数后再调用。
    """
    pass
```

**判定规则**：`destructiveHint=True` → write，否则默认 read。漏标 annotations 的 tool 会被当成 read。

### 1.5 健康探活（必须）

Gateway 每 30s 对后端 MCP 发 `ping` 探活。**无需额外开发** — MCP 标准 `ping` 方法 FastMCP 原生支持。

- 保持 server 进程存活、端口可访问即可
- 探活失败连续 3 次 → 管理界面标记「不可达」
- 验证探活可用：

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}'
```

### 2. 可观测性集成（必须）

遵循 `~/.claude/docs/observability-coding-standards.md`：

```python
# 结构化日志
import structlog
logger = structlog.get_logger()
logger.info("operation_completed", service="{{mcp-name}}", operation="xxx", result="ok")

# OTel Traces
from opentelemetry import trace
tracer = trace.get_tracer("{{mcp-name}}")

async def some_operation():
    with tracer.start_as_current_span("{{mcp-name}}.operation") as span:
        span.set_attribute("operation.type", "read")
        # ... 业务逻辑 ...

# Prometheus Metrics
from prometheus_client import Counter, Histogram
OPERATION_TOTAL = Counter("{{mcp_name}}_operations_total", "Total operations", ["operation", "status"])
```

### 3. 注册到 Gateway

开发完成后，在管理界面注册：

1. 访问 `http://localhost:8081`（gateway-admin）
2. 进入「Servers」页面，添加本 MCP：
   - Name: `{{mcp-name}}`（无下划线）
   - URL: `http://localhost:8000/mcp`
   - Description: `{{描述}}`
3. 注册时 Gateway 自动拉取 `tools/list`，识别每个 tool 的读/写和描述，展示在详情页
4. Gateway 自动探活（`ping`），展示健康状态
5. 创建 API Token 并配置本 MCP 的 read/write 权限

### 4. Client 连接配置

通过 Gateway 访问：

```json
{
  "mcpServers": {
    "gateway": {
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

直连（开发调试）：

```json
{
  "mcpServers": {
    "{{mcp-name}}": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

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
| `OTEL_EXPORTER_OTLP_ENDPOINT` | - | OTel collector（可选） |
| `PROMETHEUS_PORT` | `9464` | Prometheus metrics 端口 |

## 代码注释规范

遵循 OBS-CORE-005：注释写"为什么"不写"做了什么"。

```python
# ✅ 正确：解释为什么
# NOTE: Zabbix 6.4 problem.get 不支持 clock 排序，改用 eventid（越大越新）
params["sortfield"] = "eventid"

# ❌ 错误：解释做了什么
# 设置排序字段为 eventid
params["sortfield"] = "eventid"
```

## 注意事项

<!-- 开发中踩过的坑、特殊处理 -->
