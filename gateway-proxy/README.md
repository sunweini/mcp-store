# gateway-proxy

MCP 网关代理。聚合后端 MCP server，提供 token 认证 + 读写权限控制 + 全量调用审计（XADD `audit:calls` stream）+ Prometheus metrics。

## 运行

```bash
uv sync

# 需要 Redis 运行（gateway-admin 写入 server/token 配置，proxy 读取）
REDIS_URL=redis://localhost:6379/0 uv run python server.py  # :8080
```

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接地址 |
| `GATEWAY_PORT` | `8080` | HTTP 监听端口 |
| `GATEWAY_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `PROMETHEUS_PORT` | `9464` | Prometheus metrics 端口 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(空，用 Console)_ | OTLP trace 导出地址 |
| `OTEL_SERVICE_NAME` | `mcp-gateway` | OTel service.name |

## 架构

```
MCP Client ──Bearer token──> gateway-proxy:8080
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              PermissionMiddleware  mount(namespace=...)
                    │            │            │
              verify_token     routing     backend MCP
              (Redis SHA-256)  split_prefix  servers
```

**启动流程 (FastMCP lifespan):**

1. `gateway_lifespan` 在 FastMCP 的事件循环中运行（不用独立 event loop）
2. `mount_all`: 从 Redis `servers:active` 加载所有后端并 mount
3. `watch_changes`: 订阅 `server:changed` pubsub 频道，热加载
4. `PermissionMiddleware`: 拦截 `tools/call`，验证 token + 权限

**为什么用 lifespan 而非 `asyncio.new_event_loop`:**
`mcp.run()` 通过 `anyio.run()` 管理自己的事件循环。在独立 loop 中创建的 Redis
连接和 background task 无法在 server 的 loop 中使用。FastMCP lifespan 是官方
提供的 startup/teardown 机制，在 server 的事件循环内执行。

## 依赖

gateway-admin 写 Redis（注册 server/token），proxy 读 Redis 验证 + 热加载。

## 协议

MCP 2026-07-28, stateless HTTP。后端 MCP 通过 `mount(namespace=name)` 聚合。

## 测试

```bash
# 全量单元测试（使用 fakeredis，无需真实 Redis）
uv run pytest tests/ -v

# 冒烟测试（使用 fakeredis 启动 server，curl tools/list）
uv run python tests/smoke_test.py
```
