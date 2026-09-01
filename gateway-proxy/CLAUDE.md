# gateway-proxy - 开发说明

## 概述
MCP 网关代理。聚合后端 MCP server，token 认证，读写权限控制，全量调用审计（XADD stream），Prometheus metrics。

## 架构
- FastMCP mount(create_proxy(url), namespace=name) 聚合后端
- 自定义 TokenVerifier：SHA-256 比对 Redis（本地 TTL 缓存，见下）
- PermissionMiddleware：解析 {server}_{tool} 前缀，查 read/write 权限
- Registry：Redis Pub/Sub 热加载 server
- Audit：全量调用（成功+失败）XADD 至 `audit:calls` stream（MAXLEN 50000）；MySQL 落库在 gateway-admin 消费者（XREADGROUP 批量 INSERT）。proxy 不直连 MySQL（审计异步化，MySQL 移出请求路径）

## 并发加固（2026-08 实施）
- Token 缓存：本地 TTL 60s + `token:changed` 通道失效；Redis 故障缓存降级放行（防 403 风暴）
- Client 复用：create_proxy 传复用 client_factory（_mounted_clients 缓存），unmount 显式关闭
- 背压/超时：per-backend semaphore（默认 100）+ 总超时 90s（per-server call_timeout 覆盖）
- pubsub 自愈：watch_changes 断线重建订阅（server:changed + token:changed 同连接）

## 权限判定与 TOOL_REGISTRY（2026-09 实施）

- `routing.TOOL_REGISTRY[server] = {tool: mode}` 是每个工具读/写判定的**唯一权威**；
  mode 来自 `registry._introspect_tools` 的 `annotations.destructiveHint`
  （true → `write`，else `read`）。`PermissionMiddleware` 的 `tools/list` 过滤与
  `tools/call` 拦截都走 `check_permission(token_info, server, mode)`。
- **坑（2026-09 实测越权）**：`_mount_one` 在 `_introspect_tools` 失败（网络抖动/
  后端暂不可达，返回 `[]`）时，若仍 `register_tools(name, [])` 会用空 dict 覆盖
  `TOOL_REGISTRY`，导致 `get_tool_mode` 对任何工具退回默认 `'read'`——只读 token
  也能看到并调用 write 工具。修复：只有 tools **非空**才 `register_tools` 并写
  redis `servers:{name}.tools`；失败时保留上一次成功的 mode 元数据
  （日志 `introspect_empty_keep_last_tools`）。工具真正清零由 `_unmount_one` 的
  `clear_tools` 负责。
- 排查：出现"只 read 权限却看到/调用 write 工具"，先查 proxy 日志
  `introspect_failed` / `introspect_empty_keep_last_tools`，多半是 TOOL_REGISTRY 被
  一次瞬时故障污染，重启 proxy 重新 introspect 即恢复。

## 本地开发
```bash
uv sync
REDIS_URL=redis://localhost:6379/0 uv run python server.py
uv run pytest tests/ -v
```

## 配置
| 环境变量 | 默认 | 说明 |
|---|---|---|
| GATEWAY_PORT | 8080 | 监听端口 |
| REDIS_URL | redis://localhost:6379/0 | Redis（配置/状态/audit:calls stream） |
| PROMETHEUS_PORT | 9464 | metrics 端口 |
| OTEL_EXPORTER_OTLP_ENDPOINT | (空=console) | OTel collector |

> 注：proxy **不再直连 MySQL**（审计异步化后 MYSQL_URL 已移除，落库全在 gateway-admin 消费者侧）。

## 知识库
查 `../knowledge-base/fastmcp-v4/`：19-middleware（拦截）、50-authorization、53-token-verification。
