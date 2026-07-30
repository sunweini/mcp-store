# gateway-admin

MCP 网关管理面。FastAPI 管理 API（server/token/dashboard）+ Vue 3 静态前端。

## 运行

```bash
uv sync
REDIS_URL=redis://localhost:6379/0 JWT_SECRET=your-secret \
  uv run uvicorn app:app --port 8081 --reload
```

默认管理员：admin / admin123（首次启动自动创建，请立即改密）

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/login | 管理员登录 -> JWT |
| GET/POST/PUT/DELETE | /api/servers | Server CRUD |
| GET | /api/servers/{name}/status | 立即探活 |
| POST | /api/servers/{name}/refresh-tools | 刷新 tools 清单 |
| GET/POST/DELETE | /api/tokens | Token CRUD |
| GET | /api/metrics/summary | 监控汇总 |
| GET | /api/metrics/by-server | 分 server 统计 |
| GET | /api/metrics/timeseries | 时间序列 |
| GET | /api/failures | 失败请求列表 |

## 依赖
与 gateway-proxy 共享 Redis。admin 写 servers/tokens，proxy 热加载。admin 读 Prometheus + audit Stream。
