# {{MCP_NAME}} — 开发说明

> **本文件是 MCP 开发模板的规范**。新建 MCP 时复制 `templates/mcp-template/` 整个目录，按本文档开发。根 `CLAUDE.md` 要求每次 MCP 开发必须遵循本文档。

## 概述

<!-- 简述这个 MCP 做什么 -->

## 架构

<!-- 关键设计决策、为什么这样实现 -->

## 依赖与安装（必须）

- FastMCP v4（`fastmcp==4.0.0b1`）+ MCP Protocol `2026-07-28`（stateless HTTP）
- Python >=3.12，包管理 uv（`--prerelease=allow`）

```bash
# 安装依赖（必须 --all-extras，否则 venv 无 pytest）
uv sync --all-extras

# 跑测试（用 python -m pytest，管道 tail 可能挂）
uv run python -m pytest tests/ -q
```

### ⚠️ uv.lock 必须用阿里云镜像（生产构建前提）

生产服务器**无法访问 files.pythonhosted.org**。uv.lock 里 URL 写死官方源会导致生产构建失败。**新建/改依赖后必须重建 lock 指向阿里云**：

```bash
rm -f uv.lock
UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock
# 验证: grep -c mirrors.aliyun.com uv.lock 应 >0; files.pythonhosted.org 应为 0
```

Dockerfile 用 `uv sync --frozen --no-dev`（lock 决定依赖；Docker 层缓存失效后必须能从阿里云下载）。

## 端口规范（必须）

**MCP server 容器内端口统一分配 9050-9500**，不映射宿主端口（与 gateway-proxy 8082 / gateway-admin 8081 分离，减少攻击面）。

- 开发前先从根 `CLAUDE.md` 端口表取**最小未用端口**，登记后再开发
- 已占用：9050 tavily-mcp / 9051 brave-mcp / 9052 serpapi-mcp / 9053 zabbix-mcp（另：3306 mysql / 6379 redis）
- `server.py` 的 `MCP_PORT` 默认值改成登记到的端口（**不要再默认 8000**）
- 同机多 MCP 时 `PROMETHEUS_PORT` 也需错开（默认 9464 会冲突）

## 知识库

开发本 MCP 时，遇到 API 不确定必须先查知识库：
- 根目录 `knowledge-base/fastmcp-v4/` — FastMCP v4 完整文档（写代码前必读对应篇目，触发规则见根 CLAUDE.md）
- `knowledge-base/search-mcp-key-pool-pattern.md` — **多 API key 池设计模式**（本 MCP 需要多 key 管理时直接复用）
- `knowledge-base/mcp-production-deployment-pitfalls.md` — 生产部署踩坑（网络/镜像/Redis 权限）

## Gateway 接入

### 0. Server 描述与命名（必须）

- **Server name**：小写字母/数字/连字符，**禁止下划线**（namespace 前缀用 `_` 切分，含下划线会路由歧义）。如 `zabbix`、`tavily-mcp`、`my-db`。
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

### 1.5 工具组织模式（推荐，踩坑验证）

- **工具实现放模块级函数**（`tools/` 子模块），`register()` 只做薄包装——测试可直接 import 函数 + 注入 mock client（参考 zabbix-mcp/tools/、tavily-mcp/tools/）
- **FastMCP v4.0.0b1 拒绝 `*args/**kwargs` 通用包装**（实测崩溃）。register() 必须显式具名包装：

```python
def register(mcp, get_client, metrics=None):
    _wrap = metrics or (lambda name: lambda f: f)
    async def _mcp_list_xxx(param: str) -> dict:
        return await list_xxx(param, client=get_client())
    _mcp_list_xxx.__doc__ = list_xxx.__doc__
    mcp.tool(name="list_xxx", description=list_xxx.__doc__,
             annotations=ToolAnnotations(readOnlyHint=True))(_wrap("list_xxx")(_mcp_list_xxx))
```

- 模块级不做跨请求共享状态初始化（stateless 模式 lifespan 不可靠）——client/pool 用模块级懒加载单例

### 1.6 健康探活（必须）

Gateway 每 30s 对后端 MCP 发 `ping` 探活。**无需额外开发** — MCP 标准 `ping` 方法 FastMCP 原生支持。保持 server 进程存活、端口可访问即可。

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

**关键约束**：
- key_id / 明文 API key **禁止**入日志与 metric label（高基数 + 敏感，OBS-CORE-003）
- API key 走 URL query 的源（如 serpapi）要防 httpx INFO 日志泄漏完整 URL——把 httpx logger 提到 WARNING

### 3. 注册到 Gateway

开发完成后，在管理界面注册：

1. 访问 `http://localhost:8081`（gateway-admin）
2. 进入「Servers」页面，添加本 MCP：
   - Name: `{{mcp-name}}`（无下划线）
   - URL: `http://localhost:<登记端口>/mcp`
   - Description: `{{描述}}`
3. 注册时 Gateway 自动拉取 `tools/list`，识别每个 tool 的读/写和描述
4. Gateway 自动探活（`ping`），展示健康状态
5. 创建 API Token 并配置本 MCP 的 read/write 权限
6. 生命周期管理：Servers 页「禁用/停用/启用」——禁用=gateway 移除（容器继续跑）；停用=gateway 移除+手动停容器

### 4. Client 连接配置

通过 Gateway 访问（**端口 8082**，不是 8080）：

```json
{
  "mcpServers": {
    "gateway": {
      "url": "http://localhost:8082/mcp",
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
      "url": "http://localhost:<登记端口>/mcp"
    }
  }
}
```

### 5. 需要出网代理的 API（可选）

若后端 API 生产网络直连不通（如 api.search.brave.com），用环境变量 `SEARCH_PROXY` 控制 httpx 代理，**不要硬编码**：

```python
proxy = os.environ.get("SEARCH_PROXY") or None
client = httpx.AsyncClient(timeout=10, proxy=proxy)
```

部署时 compose 配 `${SEARCH_PROXY:-}`；admin 探活同源 API 也需同代理。

### 6. 多 API key 池（可选）

若本 MCP 需要多 key 轮换/欠费剔除/配额告警/官方用量校准，**直接复用** `knowledge-base/search-mcp-key-pool-pattern.md` 的模式（Redis schema + 配额感知轮换 + 错误分类状态机 + 热更新自愈）。参考实现：tavily-mcp / brave-mcp / serpapi-mcp 的 `key_pool.py`。

## Redis 通用坑（必读）

- **redis-py ≥6 `get_message(ignore_subscribe=True)` 参数已改名** `ignore_subscribe_messages`——旧名必 TypeError 静默失效。省略该参数 + `type=="message"` 过滤最稳
- **pubsub 连接死后不自动重连**——except 分支必须重建 pubsub（`aclose()` 旧的 → `self._redis.pubsub()` → 重新 subscribe），否则热更新永久失效只能重启进程
- gateway 存储分工：Redis 管配置/状态（server 注册/token/key 池），MySQL 管调用审计（admin 聚合/明细/失败面板）

## 本地开发

```bash
uv sync --all-extras
uv run python server.py
uv run python -m pytest tests/ -q
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | 监听地址（容器内） |
| `MCP_PORT` | `905x`（登记端口） | 监听端口（9050-9500） |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis（需要共享状态时） |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | - | OTel collector（可选） |
| `PROMETHEUS_PORT` | `9464` | Prometheus metrics 端口（同机多 MCP 错开） |
| `SEARCH_PROXY` | 空 | 出网代理（需要时） |

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
