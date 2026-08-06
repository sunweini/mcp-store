# Aliyun DNS MCP

阿里云 DNS 解析管理 MCP server：托管多个阿里云账户，按账户查询/管理 DNS 解析记录（A/AAAA/CNAME/TXT/MX/NS/SRV/CAA）。经 MCP Gateway 接入，权限双维度（服务器级 read/write + 账户级 read/write）。

## 功能

- **多账户托管**：一个 MCP server 管理多个阿里云账户（AccessKey 存 Redis，Pub/Sub 热更新，无需重启）
- **账户级权限**：每个 Gateway token 只允许访问授权账户，且读写分离
- **DNS 管理**：列出域名、查询/新增/修改/删除解析记录
- **写操作显式标注**：`⚠️ 写操作` 标记走用户确认流程，delete 不可撤销

## 工具

| Tool | 模式 | 参数 | 说明 |
|---|---|---|---|
| `list_accounts` | read | - | 当前 token 可访问的账户及读写权限 |
| `list_domains` | read | `account_id` | 账户下的域名列表（前 100） |
| `list_records` | read | `account_id`, `domain_name` | 指定主域名的解析记录列表（前 100） |
| `add_record` | write | `account_id`, `domain_name`, `rr`, `type`, `value`, `ttl`(默认 600), `priority` | 新增解析记录 |
| `update_record` | write | `account_id`, `record_id`, `rr?`, `type?`, `value?`, `ttl?`, `priority?` | 修改解析记录（至少一个字段） |
| `delete_record` | write | `account_id`, `record_id` | 删除解析记录（不可撤销） |

> `⚠️ 写操作` 工具调用前 AI 必须向用户确认参数。record_id 可从 `list_records` 结果获取。

## 权限模型

两个维度叠加，账户级为准：

1. **服务器级**（gateway）：token 对 `aliyun-dns-mcp` 的 read/write（授权矩阵 union）
2. **账户级**（本 MCP 权威）：token 对每个 `account_id` 的 read/write（`aliyndns:token_accounts:{token_id}`）

调用写工具需要账户级 write；写 ⇒ 读（不变式，MCP 侧防御式判定）。

## 接入 MCP Gateway

1. **注册 server**：`http://localhost:8081` → Servers → 添加
   - name：`aliyun-dns-mcp`（小写+连字符，禁下划线）
   - URL：容器内 `http://aliyun-dns-mcp:9054/mcp`（或本地 `http://localhost:9054/mcp`）
   - 描述：`阿里云 DNS 解析管理（多账户）`
2. **创建 token**：选择 `aliyun-dns-mcp`，勾选 read/write
3. **配置账户授权**：管理界面按账户配置该 token 的 read/write（保存即热更新，无需重启 MCP）
4. **客户端连接**：

```json
{
  "mcpServers": {
    "gateway": {
      "url": "http://localhost:8082/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

## 配置

| 环境变量 | 默认值 | 必填 | 说明 |
|---|---|---|---|
| `REDIS_URL` | - | ✅ | Redis（账户凭证 + 授权 + 热更新 pubsub） |
| `MCP_HOST` | `0.0.0.0` | | 监听地址 |
| `MCP_PORT` | `9054` | | MCP 端口 |
| `LOG_FORMAT` | `console` | | `console` / `json` |
| `PROMETHEUS_PORT` | `9464` | | Prometheus /metrics |
| `OTEL_SERVICE_NAME` | `aliyun-dns-mcp` | | 服务名 |

## 本地运行

```bash
uv sync --all-extras
redis-server --daemonize yes
REDIS_URL=redis://localhost:6379/0 uv run python server.py
uv run python -m pytest tests/ -q   # 测试
```

## 安全说明

- AccessKey/Secret/token 明文只存内网 Redis 与内存，禁入日志与 metric label
- httpx 请求日志强制提到 WARNING 级（SDK RPC URL query 含 AccessKeyId）
- 容器不映射宿主端口，仅 gateway 内网可达
- 账户级授权重复校验为防御纵深（防绕过 gateway 直连）

## 监控

- 指标：`http://localhost:9464/metrics` — `aliyndns_requests_total` / `aliyndns_request_duration_seconds` / `aliyndns_errors_total` / `aliyndns_dependency_duration_seconds` / `aliyndns_dependency_errors_total` / `aliyndns_in_flight_requests`
- 日志：structlog 结构化 key=value，`LOG_FORMAT=json` 可对接 Loki/ELK
