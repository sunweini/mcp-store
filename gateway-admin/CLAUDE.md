# gateway-admin - 开发说明

## 概述
MCP 网关管理面。FastAPI 管理 API（server/token/dashboard/calls）+ Vue 3 静态前端。与 gateway-proxy 共享 Redis（配置/状态）+ MySQL（调用审计）。

## 架构
- FastAPI + APIRouter（servers/tokens/dashboard）
- JWT 管理员认证（bcrypt + PyJWT）
- 写 Redis（server/token/admin），proxy 通过 Pub/Sub 热加载
- 读 MySQL（calls 表：聚合统计 + 请求明细 + 失败面板/轨迹）；审计消费者 XREADGROUP `audit:calls` 批量落库（lifespan 后台 task），Redis 仅 servers/tokens

## 审计消费者（2026-08 实施）
- lifespan 启动 `audit_consumer._run_consumer()`：XREADGROUP batch=100/block=1s → executemany INSERT calls → XACK
- 落库失败连续 3 次 → batch 移 `audit:calls:dead` 死信流（XACK 防无限重试）
- 查询只读 MySQL calls 表，禁读 Redis stream

## 本地开发
```bash
uv sync
REDIS_URL=redis://localhost:6379/0 JWT_SECRET=dev uv run uvicorn app:app --port 8081 --reload
uv run pytest tests/ -v
```

## 配置
| 环境变量 | 默认 | 说明 |
|---|---|---|
| ADMIN_PORT | 8081 | 监听端口 |
| REDIS_URL | redis://localhost:6379/0 | Redis（配置/状态/audit:calls stream 消费） |
| MYSQL_URL | mysql://mcp:pass@mysql:3306/mcp_audit | MySQL（调用审计） |
| JWT_SECRET | (必填) | JWT 签名密钥 |
| JWT_EXPIRES | 86400 | JWT 有效期秒 |
| GATEWAY_PROXY_METRICS_URL | http://localhost:9464/metrics | Prometheus |

## 共享 Redis schema
见根 CLAUDE.md + gateway-proxy。admin 写 servers/tokens/admin，读 MySQL calls 表（聚合/明细/失败面板）。
