# 全量调用明细审计设计（MySQL 方案）

日期：2026-08-04
状态：待审阅
相关：`2026-07-30-mcp-gateway-design.md`、`audit.py`（现有失败审计，Redis）

## 背景

gateway-proxy 现有审计只记**失败请求**（Redis Stream `audit:failures`），成功调用仅 Prometheus 聚合计数（内存、重启清零）。监控面板无逐条调用明细，聚合计数重启即丢，排障与用量审计缺数据。

目标：用 **MySQL** 记录所有 tools/call（成功+失败）元数据，dashboard 聚合统计与明细面板都从 MySQL 查--重启不丢、SQL 聚合原生支持。

## 架构决策（已确认）

- **新增 MySQL 实例**（docker-compose 加 mysql:8 容器），专管调用审计日志
- **Redis 不动**：继续管 server 注册 / token / key 池 / `audit:failures`（现有失败面板依赖，保留）
- **职责分离**：
  - Redis = 配置与状态（热数据、低延迟读写）
  - MySQL = 调用审计日志（冷写、聚合查询、持久化）
- **dashboard 聚合改读 MySQL**（不再查 Prometheus）--解决重启清零；Prometheus 保留作实时观测辅助（in-flight 等），dashboard 不再依赖
- **失败双写**：失败调用同时写 Redis `audit:failures`（现有失败面板）+ MySQL `calls` 表（新请求日志）；成功仅写 MySQL

## 数据流

```
client -> proxy on_call_tool
            ├─ 成功 -> MySQL calls 表 INSERT (status=ok)
            └─ 失败 -> Redis audit:failures（现有不变）
                     + MySQL calls 表 INSERT (status=fail, error_type)
admin:
  /api/calls        <- MySQL calls 表（明细，分页）
  /api/metrics/*    <- MySQL 聚合查询（替换 Prometheus，重启不丢）
  /api/failures     <- Redis audit:failures（现有不变）
```

## MySQL Schema

库 `mcp_audit`，表 `calls`：

```sql
CREATE TABLE IF NOT EXISTS calls (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  time DATETIME(3) NOT NULL,
  server VARCHAR(64) NOT NULL,
  tool VARCHAR(128) NOT NULL,
  op VARCHAR(8) NOT NULL DEFAULT 'read',
  token_name VARCHAR(128) NOT NULL DEFAULT '',
  latency_ms INT NOT NULL DEFAULT 0,
  status VARCHAR(8) NOT NULL,
  error_type VARCHAR(32) NOT NULL DEFAULT '',
  trace VARCHAR(64) NOT NULL DEFAULT '',
  INDEX idx_time (time),
  INDEX idx_server (server),
  INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf88mb4;
```

- `time DATETIME(3)` 毫秒精度
- 索引：time（时间窗/时间线）、server（分 server 统计）、status（按状态过滤）
- 字符集 utf8mb4（token_name 可能含中文）

## 部署

docker-compose 加 mysql 服务（容器内 3306，不映射宿主，与 Redis 同模式）：

```yaml
mysql:
  image: mysql:8.0
  environment:
    MYSQL_ROOT_PASSWORD: <from config>
    MYSQL_DATABASE: mcp_audit
    MYSQL_USER: mcp
    MYSQL_PASSWORD: <from config>
  volumes:
    - ./data/mysql:/var/lib/mysql
    - ./config/mysql-init:/docker-entrypoint-initdb.d  # 建表 SQL
  networks: [mcp-net]
  restart: unless-stopped
```

- gateway-proxy / gateway-admin 加 `MYSQL_URL`（如 `mysql://mcp:pass@mysql:3306/mcp_audit`）
- 建表 SQL 放 `deploy/config/mysql-init/01_calls.sql`（mysql 镜像首启自动执行）
- 依赖：gateway-proxy + gateway-admin 的 pyproject 加 `aiomysql`

## 实现

### gateway-proxy

**audit.py**：`record_call` 改写 MySQL（aiomysql 连接池）：

```python
import aiomysql, os, time

_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=..., port=3306, user=..., password=..., db="mcp_audit",
            minsize=2, maxsize=10, autocommit=True,
        )
    return _pool

async def record_call(meta: dict, status: str, error_type: str | None = None) -> None:
    """旁路审计：写入失败仅记日志，不阻断主请求。"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO calls (time, server, tool, op, token_name, "
                    "latency_ms, status, error_type, trace) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (meta["time"], meta["server"], meta["tool"], meta["op"],
                     meta["token_name"], meta["latency_ms"], status,
                     error_type or "", meta["trace_id"]),
                )
    except Exception as e:
        logger.error("audit_call_write_failed", error=str(e), service="gateway-proxy")
```

**middleware.py**：加 `record_call_audit(token_info, mcp_name, latency_ms, trace_id, status, error_type=None)` 辅助（构造 meta + 调 record_call），镜像现有 `record_call_failure`。

**permission_middleware.py on_call_tool**：三路径补 record_call_audit（成功 status=ok；拒绝/异常 status=fail + error_type）。失败路径仍保留 record_call_failure（写 Redis audit:failures）。

### gateway-admin

**db.py（新建）**：aiomysql 连接池单例（同 proxy 的 get_pool 模式）。

**api/calls.py（新建）**：`GET /api/calls?server=&status=&limit=&offset=` -> SQL 分页

```sql
SELECT * FROM calls
WHERE (? IS NULL OR server = ?) AND (? IS NULL OR status = ?)
ORDER BY id DESC LIMIT ? OFFSET ?
```

**api/dashboard.py 改写**：现有 `/api/metrics/summary`、`/api/metrics/by-server`、`/api/metrics/timeseries` 从查 Prometheus 改为 MySQL 聚合：

- summary：`SELECT COUNT(*), SUM(status='fail'), AVG(latency_ms) FROM calls WHERE time > DATE_SUB(NOW(), INTERVAL 24 HOUR)`
- P95：`SELECT latency_ms FROM calls WHERE time > ? ORDER BY latency_ms LIMIT 1 OFFSET ?`（offset = floor(count * 0.05)，近似 P95）
- by-server：`SELECT server, COUNT(*), SUM(status='fail') FROM calls WHERE time > ? GROUP BY server`
- timeseries：`SELECT FROM_UNIXTIME(FLOOR(UNIX_TIMESTAMP(time)/60)*60) AS bucket, COUNT(*), SUM(status='fail') FROM calls WHERE time > ? GROUP BY bucket`

`/api/failures` 保留读 Redis audit:failures（不变）。

### 前端

- 新增「请求日志」页（Calls.vue）：表格（时间/Server/Tool/Token/操作/耗时/状态）+ 过滤（server/status）+ 分页
- 监控面板（Dashboard.vue）：UI 不变，数据源切到 MySQL 聚合（API 响应结构保持兼容，前端无需改）

## 数据保留

- `calls` 表按时间清理：`DELETE FROM calls WHERE time < DATE_SUB(NOW(), INTERVAL 30 DAY)`
- 执行时机：admin 启动时跑一次 + 每日定时（admin 加 background task）
- 30 天保留足够排障与趋势分析；量大可调

## 错误处理

- MySQL 写入失败：仅记日志（`audit_call_write_failed`），不阻断请求（审计是旁路）
- MySQL 不可用：proxy 主流程不受影响（record_call try/except）；dashboard 聚合 API 返回空 + 500 日志
- 连接池断线：aiomysql 自动重连

## 测试

- audit.py: record_call INSERT + 旁路不抛 + 连接池
- middleware: on_call_tool 三路径写 calls 表 + failures 仍写 Redis
- api/calls.py: 分页/过滤/空表/鉴权
- dashboard.py: 聚合 SQL 正确性（summary/P95/by-server/timeseries）
- 现有 on_call_tool 测试无回归

## 部署影响

- docker-compose 加 mysql:8 容器（+ data 卷 + init SQL）
- gateway-proxy / gateway-admin 加 aiomysql 依赖 + MYSQL_URL 配置
- 重建 proxy + admin 容器
- 新增持久卷 `./data/mysql`

## 非目标

- 不审计 tools/list/ping（只记 tools/call）
- 不记请求参数/响应内容（元数据 only）
- 不迁移 audit:failures 到 MySQL（保留 Redis，现有失败面板不动）
- 不移除 Prometheus（保留作实时观测，dashboard 不再依赖）

## 架构变化（需同步到所有文档）

```
MCP Client -> gateway-proxy:8082 ──-> 各 MCP server
                  │
       ┌──────────┴──────────┐
       ↓                     ↓
   Redis（配置/状态）      MySQL（调用审计）
   - servers 注册          - calls 表（全量 tools/call）
   - tokens                - 聚合统计源
   - key 池（search:keys）
   - audit:failures（失败流）
gateway-admin:8081 -> Redis + MySQL
```

端口规范新增：MySQL 容器内 3306（不映射宿主）。
