# gateway-proxy - 开发说明

## 概述
MCP 网关代理。聚合后端 MCP server，token 认证，读写权限控制，失败审计，Prometheus metrics。

## 架构
- FastMCP mount(create_proxy(url), namespace=name) 聚合后端
- 自定义 TokenVerifier：SHA-256 比对 Redis
- PermissionMiddleware：解析 {server}_{tool} 前缀，查 read/write 权限
- Registry：Redis Pub/Sub 热加载 server
- Audit：失败写 Redis Stream（audit:failures）；全量调用写 MySQL（calls 表，聚合+明细）

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
| REDIS_URL | redis://localhost:6379/0 | Redis（配置/状态/失败审计） |
| MYSQL_URL | mysql://mcp:pass@mysql:3306/mcp_audit | MySQL（调用审计 calls 表） |
| PROMETHEUS_PORT | 9464 | metrics 端口 |
| OTEL_EXPORTER_OTLP_ENDPOINT | (空=console) | OTel collector |

## 知识库
查 `../knowledge-base/fastmcp-v4/`：19-middleware（拦截）、50-authorization、53-token-verification。
