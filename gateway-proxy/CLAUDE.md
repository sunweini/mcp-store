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
