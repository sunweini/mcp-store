# Zabbix MCP 设计文档

> 日期：2026-07-29
> 状态：设计完成，待实现
> 协议：MCP 2026-07-28 / FastMCP 4.0.0b1
> Zabbix 版本：6.4.7

---

## 1. 概述

Zabbix MCP 是一个 Model Context Protocol server，为 AI agent 提供 Zabbix 监控系统的操作能力。

### 核心功能

| 功能域 | 说明 |
|---|---|
| 告警巡检 | 查询活跃告警 + 生成摘要报告 |
| 维护期管理 | 创建/查看/删除维护期（含周期性） |
| 告警确认 | 查询未确认告警 + 单条/批量确认 |

### 使用者模式

**混合模式**：读操作自动执行，写操作需人工确认。

---

## 2. 架构

### 方案选择

采用**方案 B：模块化单 server**。一个 FastMCP server，代码按领域分模块。

选择理由：单 Zabbix 实例、3 个功能域 — 模块化在清晰度和简洁度之间最佳。

### 目录结构

```
zabbix-mcp/
├── CLAUDE.md                  # MCP 级开发说明
├── README.md                  # 功能说明（给用户）
├── RELEASE.md                 # 发布指南
├── pyproject.toml             # uv 依赖管理
├── server.py                  # FastMCP 入口
├── zabbix_client.py           # Zabbix JSON-RPC API 封装
├── tools/
│   ├── __init__.py            # register_tools(mcp) 统一注册
│   ├── problems.py            # 告警巡检 tool
│   ├── maintenance.py         # 维护期 CRUD tool
│   └── events.py              # 告警确认 tool
└── tests/
    ├── conftest.py            # 共享 fixtures
    ├── test_problems.py
    ├── test_maintenance.py
    └── test_events.py
```

### 模块职责

| 模块 | 职责 | 依赖 |
|---|---|---|
| `server.py` | FastMCP 实例化 + stateless HTTP + lifespan 初始化 Zabbix client | fastmcp, zabbix_client |
| `zabbix_client.py` | 封装 Zabbix JSON-RPC API（认证、请求、错误映射、OTel span） | httpx, structlog, opentelemetry |
| `tools/problems.py` | `list_active_problems` + `problem_summary` | zabbix_client |
| `tools/maintenance.py` | `create_maintenance` + `list_maintenances` + `delete_maintenance` | zabbix_client |
| `tools/events.py` | `list_unacknowledged` + `acknowledge_event` + `batch_acknowledge` | zabbix_client |

### 数据流

```
AI Agent / Claude Desktop
        │
        ▼  MCP (2026-07-28, stateless HTTP)
   ┌─────────┐
   │server.py│── lifespan ──▶ ZabbixClient(初始化)
   └────┬────┘
        │ tool call
        ▼
   tools/*.py ──▶ ZabbixClient.call(method, params)
        │
        ▼  HTTP POST (JSON-RPC 2.0)
   Zabbix Server 6.4.7
   /api_jsonrpc.php
```

---

## 3. 连接 & 认证

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| Zabbix API URL | `ZABBIX_URL` | 无（必填） | 如 `http://zabbix:8080/api_jsonrpc.php` |
| API Token | `ZABBIX_TOKEN` | 无（必填） | Zabbix 5.4+ API Token |
| 超时 | `ZABBIX_TIMEOUT` | `30` | HTTP 请求超时（秒） |
| 监听地址 | `MCP_HOST` | `127.0.0.1` | MCP server 监听地址 |
| 监听端口 | `MCP_PORT` | `8000` | MCP server 监听端口 |

认证方式：API Token 放 JSON-RPC `auth` 字段。无需 `user.login`，适配无状态协议。

---

## 4. Zabbix Client 层

### 核心接口

```python
class ZabbixClient:
    """Zabbix JSON-RPC 2.0 API 客户端。
    
    使用 API Token 认证（Zabbix 5.4+），
    不依赖 user.login session，适配无状态 MCP 协议。
    """

    def __init__(self, url: str, token: str, timeout: float = 30.0): ...
    async def call(self, method: str, params: dict) -> Any: ...
    async def close(self): ...
```

### 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| HTTP client | `httpx.AsyncClient` | async、连接池复用、timeout 控制 |
| 认证 | API Token in `auth` field | Zabbix 5.4+ 支持，无需 `user.login` |
| 错误映射 | Zabbix error → Python 异常 | tool 层拿语义化异常，不解析 JSON-RPC error |
| 重试 | 不重试 | 写操作不能盲目重试，读操作由 tool 决定是否重试 |

### 错误类型

```python
class ZabbixAPIError(Exception):         # API 返回 error（业务错误）
class ZabbixAuthError(Exception):        # 401/403（认证失败）
class ZabbixConnectionError(Exception):  # 网络不通
```

### Severity 映射

```python
SEVERITY_MAP = {
    0: "not_classified",
    1: "information",
    2: "warning",
    3: "average",
    4: "high",
    5: "disaster",
}
```

Tool 接受英文 severity 名，返回时同时带数字和名称。

---

## 5. Tool 定义

### 5.1 告警巡检（`tools/problems.py`）

#### `list_active_problems`

```python
@mcp.tool
async def list_active_problems(
    severity: str | None = None,      # "warning"|"average"|"high"|"disaster"
    host_group: str | None = None,    # 主机组名过滤
    host: str | None = None,          # 主机名过滤
    limit: int = 50,
) -> list[dict]:
    """查询当前未恢复的活跃告警，按时间降序（最新在前）。
    
    返回每条告警的：主机名、触发器描述、严重级别、发生时间、持续时间、是否已确认。
    """
```

Zabbix API: `problem.get`
- `sortfield`: `clock`
- `sortorder`: `DESC`
- `recent`: `true`
- 按 severity/host/hostgroup 过滤

#### `problem_summary`

```python
@mcp.tool
async def problem_summary() -> dict:
    """生成告警摘要报告。
    
    返回：
    - total: 活跃告警总数
    - by_severity: 按严重级别分布 {disaster: 2, high: 5, ...}
    - by_host_group: 按主机组分布
    - top_hosts: TOP 10 告警最多主机
    - unacknowledged: 未确认告警数
    """
```

Zabbix API: `problem.get`（全量） + 客户端聚合

### 5.2 维护期（`tools/maintenance.py`）

#### `create_maintenance`

```python
@mcp.tool
async def create_maintenance(
    name: str,
    host_names: list[str] | None = None,
    host_group_names: list[str] | None = None,
    start_time: str = ...,       # ISO 8601
    end_time: str = ...,         # ISO 8601
    description: str = "",
    recurring: str | None = None,          # "daily"|"weekly"|"monthly"
    recurring_days: list[int] | None = None,
    recurring_start: str | None = None,    # "02:00"
    recurring_end: str | None = None,      # "06:00"
) -> dict:
    """创建维护期。
    ⚠️ 写操作 — 执行前必须向用户确认参数（主机、时间范围）后再调用。
    
    host_names 和 host_group_names 至少传一个。
    支持一次性维护 + 周期性维护（如每周二凌晨 2-6 点）。
    """
```

Zabbix API: 
- 主机名 → `host.get` 解析为 hostid
- 主机组名 → `hostgroup.get` 解析为 groupid
- `maintenance.create`

#### `list_maintenances`

```python
@mcp.tool
async def list_maintenances(active_only: bool = True) -> list[dict]:
    """查看维护期列表。返回名称、关联主机、时间范围、状态。"""
```

Zabbix API: `maintenance.get`

#### `delete_maintenance`

```python
@mcp.tool
async def delete_maintenance(maintenance_id: str) -> dict:
    """删除/结束维护期。
    ⚠️ 写操作 — 执行前必须向用户确认后再调用。
    """
```

Zabbix API: `maintenance.delete`

### 5.3 告警确认（`tools/events.py`）

#### `list_unacknowledged`

```python
@mcp.tool
async def list_unacknowledged(
    severity: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """查询未确认的活跃告警。返回 event_id 供确认使用。"""
```

Zabbix API: `problem.get` with `acknowledged: false`

#### `acknowledge_event`

```python
@mcp.tool
async def acknowledge_event(
    event_id: str,
    message: str = "",
    close: bool = False,
) -> dict:
    """确认单条告警。
    ⚠️ 写操作 — 执行前必须向用户确认后再调用。
    
    message 记录确认原因（如"已计划维护"、"已知问题"）。
    close=True 同时关闭问题。
    """
```

Zabbix API: `event.acknowledge`

#### `batch_acknowledge`

```python
@mcp.tool
async def batch_acknowledge(
    event_ids: list[str],
    message: str = "",
    close: bool = False,
) -> dict:
    """批量确认多条告警。
    ⚠️ 写操作 — 执行前必须向用户确认后再调用。
    
    适用于同一 trigger/host 引发的多条关联告警。
    返回每条确认结果。
    """
```

Zabbix API: `event.acknowledge` with `eventids: [...]`

### Tool 汇总

| Tool | 类型 | 需确认 | Zabbix API |
|---|---|---|---|
| `list_active_problems` | 读 | ❌ | `problem.get` |
| `problem_summary` | 读 | ❌ | `problem.get` + 聚合 |
| `create_maintenance` | 写 | ✅ | `maintenance.create` |
| `list_maintenances` | 读 | ❌ | `maintenance.get` |
| `delete_maintenance` | 写 | ✅ | `maintenance.delete` |
| `list_unacknowledged` | 读 | ❌ | `problem.get` (ack=false) |
| `acknowledge_event` | 写 | ✅ | `event.acknowledge` |
| `batch_acknowledge` | 写 | ✅ | `event.acknowledge` |

---

## 6. 可观测性

遵循 `~/.claude/docs/observability-coding-standards.md`。

### 结构化日志

- 使用 `structlog`，禁止 f-string 日志
- 每条日志自动注入 `service`、`trace_id`、`span_id`
- 错误日志必带 `error` key
- 日志级别：ERROR（API 错误）、WARN（重试/降级）、INFO（正常流程）、DEBUG（开发调试）

### Trace

- 每次 Zabbix API 调用创建 Span: `zabbix_client.{method}`
- Span Attributes: `http.method`, `http.url`, `http.status_code`, `zabbix.method`
- 错误时: `record_exception` + `SetStatus(Error)` 同时

### 依赖

```toml
"structlog>=24.0",
"opentelemetry-api",
"opentelemetry-sdk",
"opentelemetry-exporter-otlp-proto-http",
```

---

## 7. 错误处理

### 错误流转

```
Zabbix API 返回 error
  → ZabbixClient 抛 ZabbixAPIError(msg)
  → Tool 层捕获 → 返回 {"status": "error", "message": "..."}

网络不通
  → ZabbixConnectionError
  → Tool 层返回 {"status": "error", "message": "Zabbix 连接失败"}

参数错误（主机不存在等）
  → ZabbixAPIError ("No permissions")
  → Tool 转中文友好提示
```

### 返回格式

```python
# 成功
{"status": "ok", "data": [...], "count": 5}

# 失败
{"status": "error", "message": "主机 'web-01' 不存在", "zabbix_error": "..."}
```

不抛异常到 MCP 层 — 返回结构化错误让 AI 理解并告知用户。

---

## 8. 安全模型

### 混合模式

读操作自动执行，写操作通过 **tool 描述标注** 约束 AI 行为：

```python
"""创建维护期。
⚠️ 写操作 — 执行前必须向用户确认参数（主机、时间范围）后再调用。
"""
```

AI agent 读到 `⚠️ 写操作` → 走 elicitation 流程让用户确认 → 确认后再调用。

不在 FastMCP 层做硬拦截 — 无状态协议不适合 session-based 确认流程。

### 输入校验

- 时间格式：ISO 8601 → Unix timestamp，格式错误抛 ValueError
- `create_maintenance` 的 `host_names` / `host_group_names` 至少传一个
- `severity` 参数校验枚举值

---

## 9. 测试策略

| 层 | 测什么 | 怎么测 |
|---|---|---|
| ZabbixClient 单元 | JSON-RPC 序列化、错误映射 | `httpx` MockTransport |
| Tool 单元 | 参数校验、返回格式、错误路径 | Mock ZabbixClient |
| 集成（可选） | Server + Client 完整链路 | FastMCP Client，需真实 Zabbix |

Mock 策略：`tests/conftest.py` 提供 `mock_zabbix` fixture，mock Zabbix API 响应。

集成测试标记 `@pytest.mark.integration`，CI 默认跳过。

---

## 10. Zabbix API 参考

基于 Zabbix 6.4 API 文档：https://www.zabbix.com/documentation/6.4/zh/manual/api

| 功能 | API 方法 |
|---|---|
| 告警查询 | `problem.get` |
| 事件确认 | `event.acknowledge` |
| 维护期 CRUD | `maintenance.create` / `maintenance.get` / `maintenance.update` / `maintenance.delete` |
| 主机查询 | `host.get` |
| 主机组查询 | `hostgroup.get` |
| 触发器查询 | `trigger.get` |
