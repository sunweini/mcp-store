# MCP Gateway 设计文档

> 日期：2026-07-30
> 状态：设计完成，待实现
> 协议：MCP 2026-07-28 / FastMCP 4.0.0b1

---

## 1. 概述

MCP Gateway 为多个 MCP server 提供统一的入口、认证授权、监控管理。

### 核心功能

| 功能域 | 说明 |
|---|---|
| MCP 代理 | 聚合多个 MCP server，统一入口 |
| 认证授权 | Token 验证，基于读写权限控制 |
| 管理界面 | Server 注册、Token 管理、监控面板 |
| 可观测性 | Prometheus metrics + OTel traces |

### 架构方案

**方案 B：分离服务**

- `gateway-proxy`：MCP 协议代理（FastMCP 4.0）
- `gateway-admin`：管理 API + Vue 3 前端（FastAPI）
- 共享存储：Redis
- 监控：Prometheus + OTel

---

## 2. 架构总览

```
                          ┌─────────────────────────┐
  MCP Client ────────────▶│   gateway-proxy/         │
  (Claude Desktop etc)    │   FastMCP Proxy          │
  Authorization: Bearer   │   MCP 协议聚合 + 路由     │
  <token>                 │   :8080/mcp              │
                          └──────────┬──────────────┘
                                     │ 验证 token
                                     ▼
                          ┌─────────────────────────┐
                          │   Redis                  │
                          │   servers / tokens /     │
                          │   admin / metrics        │
                          └──────────┬──────────────┘
                                     │
  管理员浏览器 ──────────▶┌──────────┴──────────────┐
  (Vue 3 SPA)            │   gateway-admin/         │
  admin:8081             │   FastAPI 管理 API        │
                         │   + Vue 3 静态文件        │
                         │   :8081                  │
                         └──────────┬──────────────┘
                                    │ 管理
                                    ▼
                          ┌─────────────────────────┐
                          │  zabbix-mcp :8000        │
                          │  github-mcp :8001        │
                          │  ...                     │
                          └─────────────────────────┘
```

### 两个服务

| 服务 | 职责 | 端口 |
|---|---|---|
| `gateway-proxy` | MCP 协议代理、token 验证、路由、metrics | 8080 |
| `gateway-admin` | 管理 API + Vue 3 前端 | 8081 |

### 共享存储

- **Redis**：server 列表、API token、管理员账号、metrics

---

## 3. Token 权限模型

### Token 结构

```json
{
  "token": "tok_abc123",
  "name": "zabbix-readonly",
  "permissions": {
    "zabbix": {"read": true, "write": false},
    "github": {"read": true, "write": true}
  },
  "created_at": "2026-07-30T00:00:00Z"
}
```

### Redis 存储

```
# key 用 token 的 SHA-256 哈希（不落明文），查找时 hash(传入token) 比对
tokens:{sha256(token)} → Hash
  id: "tok_id_xxx"
  name: "zabbix-readonly"
  token_hash: "{sha256(token)}"
  permissions: '{"zabbix": {"read": true, "write": false}}'
  created_at: "2026-07-30T00:00:00Z"

# id → hash 反查索引（管理界面按 id 操作用）
token_id:{tok_id_xxx} → "{sha256(token)}"
```

### 权限检查逻辑

```python
async def check_permission(token_perms: dict, server_name: str, is_write: bool) -> bool:
    perm = token_perms["permissions"].get(server_name)
    if not perm:
        return False
    if is_write and not perm.get("write", False):
        return False
    return True
```

**读写判断依据：Tool annotations**

- `readOnlyHint=True` → read 操作
- `destructiveHint=True` → write 操作

---

## 4. 路由机制

### 核心：namespace 前缀路由

Gateway 用 `mount(server, namespace="<name>")` 挂载后端。FastMCP 自动给组件加前缀，消除跨 server 的 tool 重名冲突。

**挂载前后对比（FastMCP composition 规则）：**

| 组件类型 | 挂载前 | `namespace="zabbix"` 后 |
|---|---|---|
| Tool | `list_active_problems` | `zabbix_list_active_problems` |
| Prompt | `help` | `zabbix_help` |
| Resource | `data://info` | `data://zabbix/info` |
| Template | `data://{id}` | `data://zabbix/{id}` |

### 请求路由流程

```
Client 请求
  Mcp-Method: tools/call
  Mcp-Name:   zabbix_list_active_problems
      │
      ▼
┌─ Gateway ──────────────────────────────────────┐
│ 1. 解析前缀 zabbix_ → 目标 server = zabbix        │
│ 2. 查 TOOL_REGISTRY[zabbix][list_active_problems] │
│    → mode = read                                 │
│ 3. 校验 token 对 zabbix 的 read 权限              │
│    - 通过 → 继续                                  │
│    - 拒绝 → 返回 permission_denied                │
│ 4. 剥前缀 → list_active_problems                  │
│ 5. 转发到 zabbix 后端 (http://localhost:8000/mcp) │
└────────────────────────────────────────────────┘
      │
      ▼
  zabbix-mcp 处理 list_active_problems，返回结果
```

### 路由核心代码

```python
def route(mcp_name: str, token_perms: dict) -> tuple[str, str]:
    """解析目标 server + 校验权限。

    返回 (server_name, bare_tool_name)。
    权限不足时抛 PermissionDenied。
    """
    # 1. 拆前缀：zabbix_list_active_problems → ("zabbix", "list_active_problems")
    server, tool = split_prefix(mcp_name)
    if server not in TOOL_REGISTRY:
        raise NotFound(f"未知 server 前缀: {server}")

    # 2. 判断读写
    is_write = TOOL_REGISTRY[server][tool]["mode"] == "write"

    # 3. 校验 token 权限
    perm = token_perms.get(server, {})
    need = "write" if is_write else "read"
    if not perm.get(need, False):
        raise PermissionDenied(f"token 对 {server} 无 {need} 权限")

    return server, tool


def split_prefix(mcp_name: str) -> tuple[str, str]:
    """从 namespaced tool name 拆出 server 前缀和裸 tool 名。

    NOTE: server name 注册时禁止含下划线，否则前缀切分歧义。
    注册校验阶段强制 server name 用 [a-z0-9-] 字符集。
    """
    server, _, tool = mcp_name.partition("_")
    return server, tool
```

### tools/list 聚合

Client 调 `tools/list` 时，Gateway：

1. 遍历 token 有权限的所有 server
2. 拉取各自 `tools/list`，加 namespace 前缀
3. **按权限过滤**：token 对某 server 无 write 权限 → 该 server 的写 tool 不返回
4. 合并返回给 client

Client 视角：

```
zabbix_list_active_problems   (read)
zabbix_problem_summary        (read)
zabbix_create_maintenance     (write)   ← 仅当 token 有 zabbix.write 才出现
github_list_repos             (read)
```

### 为什么用前缀而非「查表反查」

| 方案 | 问题 |
|---|---|
| 按裸 tool name 反查注册表 | tool 重名冲突：zabbix 和 github 都有 `list_items`，无法区分 |
| **namespace 前缀** ✅ | `zabbix_list_items` vs `github_list_items` 天然隔离，无歧义 |
| 按 URL path 分流 | client 需配多个 endpoint，非真正统一网关 |

### 与权限模型的衔接

```
Mcp-Name 前缀  →  目标 server
                    ↓
         查 TOOL_REGISTRY 得 mode (read/write)
                    ↓
         对照 token.permissions[server][mode]
                    ↓
              放行 / permission_denied
```

---

## 5. gateway-proxy 设计

### 核心流程

```
MCP Client 请求
    │
    ├─ Header: Authorization: Bearer <token>
    ├─ Header: Mcp-Method: tools/call
    ├─ Header: Mcp-Name: list_active_problems
    │
    ▼
1. 提取 token → Redis 验证
2. 解析 tool name → 查 annotations（read/write）
3. 权限检查（read/write）
4. 转发到后端 MCP server（stateless HTTP）
5. 记录 metrics + trace span
```

### 模块划分

```
gateway-proxy/
├── CLAUDE.md
├── pyproject.toml
├── server.py          # FastMCP 4.0 入口，stateless HTTP
├── auth.py            # Token 验证（SHA-256 比对）+ 权限检查
├── routing.py         # namespace 前缀路由 + TOOL_REGISTRY
├── registry.py        # server 列表热加载（Redis Pub/Sub 订阅）
├── audit.py           # 失败请求 journey 写审计存储（Redis Stream）
├── observability.py   # OTel SDK + Prometheus 集成
└── redis_client.py    # Redis 连接
```

### server.py 核心

```python
"""MCP Gateway Proxy — FastMCP 4.0, MCP 2026-07-28, stateless."""
from fastmcp import FastMCP, create_proxy
from fastmcp.server.auth import AuthProvider

gateway = FastMCP(
    "MCP Gateway",
    auth=GatewayAuthProvider(),  # 自定义 auth provider
)

# 动态 mount 后端 server（启动时从 Redis 加载，运行时 Pub/Sub 热更新）
async def mount_servers():
    servers = await redis.smembers("servers:active")
    for name in servers:
        info = await redis.hgetall(f"servers:{name}")
        gateway.mount(create_proxy(info["url"]), name=name)

if __name__ == "__main__":
    gateway.run(transport="streamable-http", stateless_http=True, port=8080)
```

### Server 热加载机制（registry.py）

admin 写 Redis 后，proxy 通过 **Redis Pub/Sub** 感知变更，动态 mount/unmount，**无需重启**。

```
admin 注册/更新/删除 server
    │ 写 Redis + PUBLISH server:changed {"action":"add|update|remove","name":"zabbix"}
    ▼
proxy 订阅 server:changed 通道
    │
    ├─ action=add    → mount(create_proxy(url), name=name) + 建 TOOL_REGISTRY
    ├─ action=update → unmount 旧的 → 重新 mount + 刷新 TOOL_REGISTRY
    └─ action=remove → unmount + 清 TOOL_REGISTRY
```

```python
async def watch_server_changes():
    pubsub = redis.pubsub()
    await pubsub.subscribe("server:changed")
    async for msg in pubsub.listen():
        if msg["type"] != "message":
            continue
        evt = json.loads(msg["data"])
        name, action = evt["name"], evt["action"]
        if action in ("add", "update"):
            await remount_server(name)      # unmount 旧 + mount 新 + 刷 TOOL_REGISTRY
        elif action == "remove":
            await unmount_server(name)
```

**兜底**：proxy 启动时全量加载 `servers:active`；另每 60s 对账一次 Redis，防 Pub/Sub 丢消息。

### 失败审计日志（audit.py）

请求失败时，proxy 记录完整 journey 到 **Redis Stream**（`audit:failures`），供 admin 的 `/api/failures` 读取。

**埋点位置**（每个阶段记录耗时，失败即断）：

```
client 到达 → [gateway 接收] → [auth 验证] → [route 路由] → [转发 server]
                  ↓任一失败：写 journey {各阶段state+ms} + error_type 到 Redis Stream
```

```python
async def record_failure(journey: list[dict], error_type: str, meta: dict):
    await redis.xadd("audit:failures", {
        "trace": meta["trace_id"],
        "server": meta["server"], "tool": meta["tool"], "op": meta["op"],
        "error_type": error_type,
        "message": meta["message"],
        "latency_ms": meta["latency_ms"],
        "time": meta["time"],
        "journey": json.dumps(journey),   # [{stage, state, ms}, ...]
    })
    # NOTE: Stream 用 MAXLEN ~ 10000 限制，防无限增长
```

- error_type 按枚举分类：`upstream_timeout / permission_denied / invalid_token / upstream_error / connection_error`
- admin 只读消费，不写审计

### 可观测性

**Prometheus 指标（OTel SDK + Prometheus exporter，与 zabbix-mcp 一致）：**

```python
from opentelemetry import metrics

meter = metrics.get_meter("mcp-gateway")

# NOTE: label 均为有界基数（server/tool/operation/status 取值有限），
# 符合 OBS-MET-002，无 user_id/request_id 等高基数字段。
REQUESTS_TOTAL = meter.create_counter(
    "gateway_requests_total",
    description="Total MCP requests",
)
REQUEST_LATENCY = meter.create_histogram(
    "gateway_request_duration_seconds",
    description="Request latency",
)
AUTH_FAILURES = meter.create_counter(
    "gateway_auth_failures_total",
    description="Auth failures",
)

# 记录时传 attributes（低基数 label）
REQUESTS_TOTAL.add(1, {"server": server, "tool": tool, "operation": op, "status": status})
```

**OTel Traces：**

```python
with tracer.start_as_current_span(f"gateway.{server}.{tool}") as span:
    span.set_attributes({
        "gateway.server": server,
        "gateway.tool": tool,
        "gateway.operation": "read" if read_only else "write",
        "gateway.token_name": token_name,
    })
```

### 环境变量

```
GATEWAY_PORT=8080
REDIS_URL=redis://localhost:6379/0
PROMETHEUS_PORT=9464
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
OTEL_SERVICE_NAME=mcp-gateway
```

---

## 6. gateway-admin 设计

### 模块划分

```
gateway-admin/
├── CLAUDE.md
├── pyproject.toml
├── app.py               # FastAPI 入口
├── auth.py              # 管理员登录（用户名密码 + JWT）
├── api/
│   ├── servers.py       # Server CRUD + 探活 + tools 自省 API
│   ├── tokens.py        # Token CRUD + 权限配置 API
│   └── dashboard.py     # 监控 API（metrics/summary、by-server、timeseries、failures）
├── redis_client.py      # Redis 连接
└── admin-ui/            # Vue 3 前端
    ├── src/
    │   ├── views/
    │   │   ├── Login.vue
    │   │   ├── Servers.vue
    │   │   ├── Tokens.vue
    │   │   └── Dashboard.vue
    │   ├── components/
    │   └── App.vue
    ├── package.json
    └── dist/
```

### API 设计

#### 认证

```
POST /api/login
Body: {"username": "admin", "password": "xxx"}
→ {"token": "jwt_xxx", "expires_in": 86400}

后续请求：Authorization: Bearer jwt_xxx
```

#### Server 管理

```
GET    /api/servers           → Server 列表（含 tools 清单 + 健康状态）
POST   /api/servers           → 注册 server
PUT    /api/servers/{name}    → 更新 server
DELETE /api/servers/{name}    → 删除 server
GET    /api/servers/{name}/status → 立即探活
```

**Server 数据结构（Redis）：**

```
servers:zabbix → Hash
  name: "zabbix"
  url: "http://localhost:8000/mcp"
  description: "Zabbix 告警巡检 / 维护期 / 告警确认"
  status: "active"          # active / disabled
  tools: '[{name, mode, desc}]'  # 从 MCP tools/list 拉取
  last_health_check: "2026-07-30T14:00:00Z"
  health_up: "1"
  health_latency_ms: "12"
  created_at: "2026-07-30T00:00:00Z"
```

#### 后端 MCP 探活（健康检查）

**要求：每个后端 MCP server 必须可探活。**

Gateway 定期（每 30s）对每个已注册 server 发起探活：

```python
# 探活方式：MCP 标准 ping（无需认证，最轻量）
async def probe(server_url: str) -> HealthResult:
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(server_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "ping", "params": {}
            })
            latency = (time.monotonic() - start) * 1000
            return HealthResult(up=resp.status_code == 200, latency_ms=latency)
    except httpx.HTTPError:
        return HealthResult(up=False, latency_ms=None)
```

- 探活结果写入 Redis（`health_up` / `health_latency_ms` / `last_health_check`）
- Server 状态推导：连续 3 次失败 → 标记「不可达」
- 管理界面展示健康状态（绿色健康 / 红色不可达）+ 最近探活时间 + 延迟
- 「立即探活」按钮手动触发

#### Tools 自省（读写清单）

Gateway 注册/刷新 server 时，调用 `tools/list` 拉取 tools 清单：

```python
tools = await mcp_client.list_tools()
for t in tools:
    ann = t.annotations or {}
    mode = "write" if ann.destructiveHint else "read"  # 默认 read
    # 存储 {name, mode, description}
```

- 管理界面 Server 详情展示：每个 tool 的 name + 读/写徽章 + 描述
- Token 权限页面的 server 列表同样展示 tools 清单，方便配置权限时参考

#### Token 管理

```
GET    /api/tokens           → Token 列表（token 字段返回掩码，如 tok_9f3k****）
POST   /api/tokens           → 创建 token（响应一次性返回明文，此后不可再查）
DELETE /api/tokens/{id}      → 删除 token
PUT    /api/tokens/{id}      → 更新权限
```

**⚠️ Token 安全规范：**
- 存储：token 明文经 SHA-256 哈希后存 Redis，**不落明文**
- 创建时响应返回一次明文，前端提示「只显示一次，请妥善保存」
- 列表/详情接口只返回掩码（前 8 位 + `****`），**不提供 reveal 明文接口**
- 验证：proxy 收到请求时对 token 做同样哈希后比对

#### 监控面板

```
GET /api/metrics/summary?server=     → 总请求/错误/错误率/P95/读写/鉴权失败（可按 server 过滤，后端聚合）
GET /api/metrics/by-server           → 分 server 统计表
GET /api/metrics/timeseries?server=&window=  → 时间序列数组（sparkline + timeline 用）
GET /api/failures?server=&limit=&offset=     → 失败请求列表（含 journey 轨迹，分页）
GET /api/failures/{trace}            → 单条失败详情
```

### Vue 3 前端页面

- **登录页**：用户名密码
- **Server 管理**：列表 + 添加/编辑/删除
- **Token 管理**：列表 + 创建/删除 + 权限配置
- **监控面板**：统计卡片 + 图表（请求量、延迟、错误率）

### 管理员账号

**Redis 存储：**

```
admin:admin → Hash
  password_hash: "$2b$..."  # bcrypt
  role: "admin"
  created_at: "2026-07-30T00:00:00Z"
```

**环境变量：**

```
ADMIN_PORT=8081
REDIS_URL=redis://localhost:6379/0
JWT_SECRET=xxx
JWT_EXPIRES=86400
GATEWAY_PROXY_METRICS_URL=http://localhost:9464/metrics
```

---

## 7. 部署方案

### 本地开发

```bash
# 1. 启动 Redis
redis-server

# 2. 启动后端 MCP servers
cd zabbix-mcp && uv run python server.py  # :8000

# 3. 启动 gateway-proxy
cd gateway-proxy
REDIS_URL=redis://localhost:6379/0 uv run python server.py  # :8080

# 4. 启动 gateway-admin
cd gateway-admin
REDIS_URL=redis://localhost:6379/0 uv run python app.py  # :8081

# 5. 访问管理界面
open http://localhost:8081
```

### Docker Compose（生产）

```yaml
version: '3.8'

services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    volumes: ["redis-data:/data"]

  zabbix-mcp:
    build: ./zabbix-mcp
    environment:
      - ZABBIX_URL=${ZABBIX_URL}
      - ZABBIX_TOKEN=${ZABBIX_TOKEN}
    ports: ["8000:8000"]

  gateway-proxy:
    build: ./gateway-proxy
    environment:
      - REDIS_URL=redis://redis:6379/0
      - GATEWAY_PORT=8080
      - PROMETHEUS_PORT=9464
    ports:
      - "8080:8080"
      - "9464:9464"
    depends_on: [redis, zabbix-mcp]

  gateway-admin:
    build: ./gateway-admin
    environment:
      - REDIS_URL=redis://redis:6379/0
      - ADMIN_PORT=8081
      - JWT_SECRET=${JWT_SECRET}
      - GATEWAY_PROXY_METRICS_URL=http://gateway-proxy:9464/metrics
    ports: ["8081:8081"]
    depends_on: [redis, gateway-proxy]

volumes:
  redis-data:
```

---

## 8. 开发规范

### 协议规范

- MCP 2026-07-28，stateless HTTP
- FastMCP 4.0.0b1
- Tool annotations：`readOnlyHint` / `destructiveHint`

### 读写分离

所有 MCP tool 必须标注 annotations：

```python
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def read_operation(...): ...

@mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
def write_operation(...): ...
```

### 可观测性

遵循 `~/.claude/docs/observability-coding-standards.md`：

- 结构化日志：structlog key=value
- Traces：OTel span per request
- Metrics：Prometheus 核心指标

### 代码注释

遵循 OBS-CORE-005：注释写"为什么"不写"做了什么"。

---

## 9. 目录结构最终版

```
mcp-store/
├── CLAUDE.md
├── docker-compose.yml
├── gateway-proxy/
│   ├── CLAUDE.md
│   ├── pyproject.toml
│   ├── server.py
│   ├── auth.py
│   ├── routing.py
│   ├── observability.py
│   └── redis_client.py
├── gateway-admin/
│   ├── CLAUDE.md
│   ├── pyproject.toml
│   ├── app.py
│   ├── auth.py
│   ├── api/
│   │   ├── servers.py
│   │   ├── tokens.py
│   │   └── dashboard.py
│   ├── redis_client.py
│   └── admin-ui/
│       ├── src/
│       ├── package.json
│       └── dist/
├── zabbix-mcp/
├── knowledge-base/
└── templates/
```

---

## 10. API 数据契约（响应结构）

前端按以下结构消费数据，后端响应须对齐。

### GET /api/servers

```json
[
  {
    "name": "zabbix",
    "url": "http://localhost:8000/mcp",
    "description": "Zabbix 告警巡检 / 维护期 / 告警确认",
    "status": "active",
    "health": {
      "up": true,
      "latency_ms": 12,
      "last_check": "2026-07-30T14:00:00Z"
    },
    "tools": [
      { "name": "list_active_problems", "mode": "read",  "description": "查询当前活跃告警…" },
      { "name": "create_maintenance",   "mode": "write", "description": "创建维护期…" }
    ],
    "created_at": "2026-07-30T00:00:00Z"
  }
]
```

### GET /api/tokens

```json
[
  {
    "id": "tok_id_xxx",
    "name": "zabbix-readonly",
    "token_masked": "tok_9f3k****",
    "permissions": { "zabbix": { "read": true, "write": false } },
    "created_at": "2026-07-30T00:00:00Z"
  }
]
```

### POST /api/tokens（创建响应，含一次性明文）

```json
{
  "id": "tok_id_xxx",
  "name": "zabbix-readonly",
  "token": "tok_9f3kq8zabbix001",
  "warning": "明文只显示一次，请立即保存",
  "permissions": { "zabbix": { "read": true, "write": false } },
  "created_at": "2026-07-30T00:00:00Z"
}
```

### GET /api/metrics/summary

```json
{
  "requests": 12847,
  "errors": 27,
  "error_rate": 0.21,
  "p95_ms": 128,
  "read": 10432,
  "write": 2415,
  "auth_failures": 12
}
```

### GET /api/metrics/by-server

```json
[
  { "server": "zabbix",   "requests": 8042, "errors": 14, "error_rate": 0.17, "p95_ms": 120, "read": 6600, "write": 1442, "auth_failures": 3 },
  { "server": "postgres", "requests": 1245, "errors": 8,  "error_rate": 0.64, "p95_ms": 340, "read": 1032, "write": 213,  "auth_failures": 3 }
]
```

### GET /api/metrics/timeseries

```json
{ "window": "1h", "bucket": "1min", "points": [12, 18, 15, 22, 19, 28] }
```

### GET /api/failures

```json
[
  {
    "trace": "8f3a9c2e7b1d4f6a0c21d9e8b7a65432",
    "server": "zabbix",
    "tool": "list_active_problems",
    "op": "read",
    "error_type": "upstream_timeout",
    "message": "Zabbix API 请求超时（30s）…",
    "latency_ms": 30002,
    "time": "2026-07-30T14:32:18Z",
    "journey": [
      { "stage": "client",     "state": "ok",   "ms": 2 },
      { "stage": "gateway",    "state": "ok",   "ms": 1 },
      { "stage": "auth",       "state": "ok",   "ms": 3 },
      { "stage": "route",      "state": "ok",   "ms": 1 },
      { "stage": "zabbix-mcp", "state": "fail", "ms": 30002 }
    ]
  }
]
```

**error_type 枚举**：`upstream_timeout` / `permission_denied` / `invalid_token` / `upstream_error` / `connection_error`

**journey.stage 枚举**：`client` → `gateway` → `auth` → `route` → `<server>-mcp`
**journey.state 枚举**：`ok`（已通过）/ `fail`（失败点）/ `skip`（未到达）

---

## 11. 后端开发注意点

### 数据源与存储

| 数据 | 来源 | 存储 |
|---|---|---|
| server 注册 / token / admin | 管理操作 | Redis Hash |
| 请求 metrics（计数/延迟/错误） | gateway-proxy 埋点 | Prometheus（OTel SDK 暴露） |
| 失败请求 + journey | gateway-proxy 失败埋点 | Redis Stream 或落库（审计日志） |
| 时间序列 / P95 | Prometheus query | 实时查询转数组 |

### 关键实现点

1. **proxy 写、admin 读** — 两服务共享 Redis + Prometheus。proxy 负责埋点写入，admin 只读聚合展示。约定统一 key 前缀与 metric 名。

2. **metrics 聚合放后端** — `/api/metrics/summary` 返回已聚合结果，不让前端拉明细自己 reduce。

3. **时间序列查 Prometheus** — admin 调 Prometheus HTTP API（`rate()` / `histogram_quantile()`），转成前端要的数组。P95 用 histogram。

4. **失败 journey 是结构化审计日志** — proxy 在每阶段（auth/route/forward）埋点，失败时记录断点 + 各阶段耗时，写入审计存储。error_type 按枚举分类。

5. **token 存哈希** — SHA-256 后存，明文只在创建响应返回一次。验证时同样哈希比对。

6. **tools 自省可刷新** — 注册时拉 `tools/list` 存 name/mode/desc；提供 refresh 接口应对后端 tool 变更。mode 由 `annotations.destructiveHint` 判定。

7. **探活定时 + 手动** — 每 30s 定时 ping，连续 3 次失败标记不可达。`POST /api/servers/{name}/ping` 手动触发。

8. **时间返回绝对值** — ISO 8601 + Unix 时间戳，相对时间（「2 分钟前」）前端算。

9. **分页** — failures / tokens 列表支持 `limit` + `offset`。

10. **创建 token 校验 server 存在** — permissions 引用的 server 必须已注册，否则 400。

### 前端未体现但后端必需

| 项 | 说明 |
|---|---|
| gateway-proxy 独立服务 | MCP 路由 + token 验证 + 转发，与管理面分离 |
| JWT 中间件 | 管理 API 全鉴权（除 /login） |
| CORS | 前端独立部署需配置 |
| 请求日志管道 | proxy 写 metrics + 审计日志，admin 消费 |
| 实时刷新（可选） | dashboard live 需 SSE/WebSocket 推送，否则轮询 |
