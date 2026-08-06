# 阿里云 DNS 管理 MCP 设计

- 日期：2026-08-06
- 状态：设计定稿
- 关联：mcp-gateway-design（2026-07-30）、multi-search-mcp-design（2026-08-03）

## 1. 概述

新建 `aliyun-dns-mcp`：托管多个阿里云账户，按账户查询/管理 DNS 解析记录。通过 MCP Gateway 接入，权限支持两个维度：

1. **账户维度**：token 只能访问被授权的阿里云账户子集
2. **读写维度**：**账户级**——同一 token 可对账户 A 只读、对账户 B 可写（write 隐含 read）

**关键发现（零 gateway 改动前提）**：FastMCP 的 `StreamableHttpTransport` 在 proxy 场景下自动把当前 HTTP 请求的 headers 转发给后端（`client_kwargs["headers"] = get_http_headers() | self.headers`，见 fastmcp/client/transports.py），且 `get_http_headers()` 默认排除列表不含 `authorization`（见 fastmcp/server/dependencies.py）。因此 gateway 代理调用后端时 **Bearer token 已被自动透传**，per-account 授权可在 MCP 层实现，无需修改 gateway-proxy。

## 2. 架构

```
MCP Client
  └─ Bearer <gateway_token> ─→ gateway-proxy:8082
                                ├─ verify_token（SHA-256 + Redis tokens:{hash}）
                                ├─ 工具可见性粗闸（server 级 read/write，来自 union）
                                └─ proxy 自动转发（含 Authorization 头）
                                            └─→ aliyun-dns-mcp:9054
                                                  ├─ 读 Authorization → hash → tokens:{hash} → token_id
                                                  ├─ 查 aliyundns:token_accounts:{token_id} → 账户级 read/write
                                                  ├─ 校验 account_id ∈ 授权映射 + 所需 mode
                                                  ├─ 用该账户 AccessKey 调阿里云 Alidns API（SDK）
                                                  └─ Redis 账户凭证缓存 + Pub/Sub 热更新
```

组件归属：

| 组件 | 改动 | 职责 |
|---|---|---|
| aliyun-dns-mcp（新建） | 全部 | 6 tools + per-account 鉴权 + Alidns 调用 + 凭证热更新 |
| gateway-proxy | 零改动 | token 验证 + 工具可见性粗闸（现有能力） |
| gateway-admin | 新增两页 | 阿里云账户 CRUD；token×账户 read/write 授权矩阵（自动同步 union 到 gateway token） |

## 3. 权限模型（双维度，MCP 为权威）

### 3.1 数据

```
aliyndns:token_accounts:{token_id}          Hash — 账户级授权（权威）
  field: {account_id} → JSON {"read": bool, "write": bool}
  不变式：write ⇒ read（要改记录必须能查记录；UI 强制）
  MCP 侧防御式判定：read 权限 = read or write（防 Redis 手改出违反不变式的数据）

tokens:{hash}                                Hash — 现有 gateway token（union 自动同步）
  permissions["aliyun-dns-mcp"] = {"read": any_read, "write": any_write}
  由 gateway-admin 保存授权矩阵时按 union 写，保证写工具可见性
  （server 名 = 目录名 aliyun-dns-mcp，同 tavily-mcp/serpapi-mcp 注册模式）
```

### 3.2 执行

| 维度 | 位置 | 机制 |
|---|---|---|
| 账户级 read/write（权威） | aliyun-dns-mcp | 每次调用：hash Authorization → `tokens:{hash}` → token_id → `aliyndns:token_accounts:{token_id}` → 校验账户存在且 `perm[required_mode]`，否则 ToolError 明确报"无权限" |
| 工具可见性粗闸 | gateway-proxy | 现有 check_permission；union 保证有任一账户写权限的 token 能看到写工具 |

### 3.3 安全说明

- MCP 重复验证 token 是**防御纵深**而非冗余：直接访问（绕过 gateway 的部署不允许——容器不映射宿主端口）与 gateway 校验失败两条路径都被挡住
- MCP 校验失败时 account 名可入错误消息（非敏感），token/密钥永远不进错误消息
- 部署模型：MCP 只在容器内网可达，依赖"只有 gateway 能访问"这一前提；若未来直接暴露，MCP 层鉴权依然成立（凭 token 本身）

## 4. 工具集（6 个）

所有 tool 首参 `account_id`（MCP 鉴权键）；写 tool 标 `destructiveHint=True` + docstring 含 `⚠️ 写操作`。

| Tool | 模式 | 参数 | Alidns API |
|---|---|---|---|
| `list_accounts` | read | - | 无（读 Redis 授权映射） |
| `list_domains` | read | `account_id` | DescribeDomains |
| `list_records` | read | `account_id`, `domain_name` | DescribeDomainRecords |
| `add_record` | write | `account_id`, `domain_name`, `rr`, `type`, `value`, `ttl`(默认600), `priority`(MX/SRV) | AddDomainRecord |
| `update_record` | write | `account_id`, `record_id`, `rr?`, `type?`, `value?`, `ttl?`, `priority?` | UpdateDomainRecord |
| `delete_record` | write | `account_id`, `record_id` | DeleteDomainRecord |

### 4.1 list_accounts

返回 `[{account_id, description, read, write}]`，来自当前 token 的 `aliyndns:token_accounts:{token_id}` 全量（小规模，直接全量；不暴露 AccessKey）。

### 4.2 返回结构

所有 tool 返回字典：

```json
{
  "status": "ok" | "error",
  "data": ...,
  "error_type": null | "no_permission" | "account_not_found" | "aliyun_api_error" | "account_disabled" | "invalid_token" | "throttled" | "network_error",
  "message": "..."
}
```

- `list_domains` data：`[{domain_name, dns_servers, record_count}]`（取前 100，分页后续做）
- `list_records` data：`[{record_id, rr, type, value, ttl, priority, status}]`
- `add_record` data：`{record_id}`
- `update_record` data：`{record_id}`
- `delete_record` data：`{record_id}`

## 5. 数据模型（Redis）

```
aliyndns:accounts:{account_id}              Hash — 阿里云账户凭证
  access_key_id       -> "LTAI..."
  access_key_secret   -> "..."            # 明文存内网 Redis，禁入日志/metric
  description         -> "生产主账户"
  region              -> "cn-hangzhou"    # Alidns 固定，占位兼容
  enabled             -> "true"
  created_at          -> ISO8601

aliyndns:accounts:index                     Set — 全部 account_id（管理页遍历）

aliyndns:token_accounts:{token_id}          Hash — 账户级授权（见 §3.1）

aliyndns:changed                            Pub/Sub — 账户/授权变更通知
  {"action": "upsert"|"delete", "key": "aliyndns:accounts:{account_id}"|"aliyndns:token_accounts:{token_id}"}
  key 为完整 Redis key（含 aliyndns: 前缀）
```

设计要点：

- **account_id 命名**：人类可读（`prod-main`、`test-1`），是 Redis key、授权字段、tool 参数三合一
- **令牌-账户映射按 token_id 存**（非 token hash）：id 稳定可读；MCP 需先读 `tokens:{hash}` 拿 id（顺带校验 token 有效性）
- **Redis 是唯一事实源**：MCP 启动全量加载 + Pub/Sub 增量刷新（复用 search-mcp key 池模式）
- **凭证安全**：AccessKey/Secret 只在 Redis 值内明文；日志/metrics 只用 `account_id`（OBS-CORE-003）

## 6. 组件设计

### 6.1 aliyun-dns-mcp

结构（复用 zabbix/serpapi 模式）：

```
aliyun-dns-mcp/
├── server.py            # FastMCP 入口，port 9054，懒加载单例
├── logging_config.py
├── telemetry.py         # OTel traces + Prometheus（aliyndns_* 指标族）
├── aliyun_client.py     # AlidnsClient：SDK 封装，错误分类，trace span
├── account_store.py     # Redis 账户+授权加载、Pub/Sub 热更新
├── auth.py              # token 验证（hash + tokens:{hash}）+ 账户级授权检查
├── tools/
│   ├── __init__.py      # register_tools（显式具名包装）
│   ├── accounts.py      # list_accounts
│   ├── domains.py       # list_domains
│   └── records.py       # list/add/update/delete_record
└── tests/
```

关键实现点：

- **懒加载单例**（stateless 模式 lifespan 不可靠，模块级 init）：account_store、AlidnsClient 按账户缓存
- **FastMCP v4 拒绝 `*args/**kwargs` 包装**：register() 显式具名包装（模板 §1.5）
- **获取 token**：`get_http_headers(include_all=True)` 取 `authorization`（默认版会排除该头，必须 include_all）——与 gateway 注释同款坑，写测试回归
- **依赖注入**：工具函数带 `client_factory`/`auth` 参数（测试注入 mock，不进 tool schema）

### 6.2 gateway-admin（新增两页）

**页面 A：阿里云账户管理**
- CRUD：id/description/AccessKeyId/AccessKeySecret/region/enabled
- 写 `aliyndns:accounts:{id}` + index + PUBLISH `aliyndns:changed`
- 凭证字段输入掩码、禁入审计日志
- 删除账户时同步清理所有 `aliyndns:token_accounts:*` 中该账户的引用（防僵尸授权）

**页面 B：token×账户授权矩阵**
- 入口：Token 列表 → token 详情页，嵌入授权矩阵；列=**全部托管账户**（勾选=授予该 token），单元格 read/write 勾选，write 强制连带 read
- 保存时：
  1. 写 `aliyndns:token_accounts:{token_id}`（权威）
  2. 计算 union 更新 `tokens:{hash}` 的 `aliyun-dns` read/write（工具可见性）
  3. PUBLISH `aliyndns:changed`

- **探活**：添加/修改账户凭证时，用该账户凭证调 DescribeDomains（PageSize=1）验证 AccessKey 有效；失败提示、不落库（可勾选跳过）
- 探活消耗 0 配额（查询免费），与 search-mcp key 探活不同
- 探活意味着 gateway-admin 后端引入 Alidns SDK 依赖（与 MCP 共用 SDK，版本对齐）

### 6.3 gateway-proxy

零改动。依赖 FastMCP 自动 header 转发（见 §1 关键发现）。风险与兜底见 §9。

## 7. 阿里云 Alidns API（SDK）

- 依赖：`alibabacloud-alidns20150109` + `alibabacloud-tea-openapi`（RPC 签名/端点/错误解析交给 SDK）
- 认证：每账户一个 Client（该账户 AccessKey），模块级缓存，key 变化后重建
- 端点：`alidns.cn-hangzhou.aliyuncs.com`（region 固定）
- API 映射见 §4 表，方法名已从阿里云官方文档确认（Alidns/2015-01-09）

### 7.1 错误分类（参照 search-mcp key 池模式）

| 阿里云错误（示例码） | 分类 | 动作 |
|---|---|---|
| 403 / InvalidAccessKeyId / Forbidden | INVALID_CREDENTIAL | 账户标 `enabled=false` + 告警（front 提示修复） |
| Throttling（429 语义） | THROTTLED | 短退避重试 1 次（sleep 1s） |
| InvalidDomainName.NoExist 等 | NOT_FOUND | 原样报错 |
| 其余 4xx/5xx | API_ERROR | 原样报错 |
| 网络/超时 | 不分类 | **不标账户**（瞬时问题，同 key 池教训） |

> 错误码为示例，**实现时以实测为准**（阿里云 SDK 错误信息含 request_id，分类判定写测试回归）。

**内部分类 → 对外 error_type 映射**：INVALID_CREDENTIAL→`account_disabled`（并告警）；THROTTLED→`throttled`；NOT_FOUND/API_ERROR→`aliyun_api_error`；网络/超时→`network_error`；授权失败→`no_permission`；`tokens:{hash}` 查无→`invalid_token`。

## 8. 可观测性 / 错误处理 / 测试

### 8.1 可观测性（OBS 规范）

- structlog 结构化 key=value + OTel trace 注入；`LOG_FORMAT=json` 生产
- Traces：FastMCP 自动 span；AlidnsClient 每 API 调用一个 span（`aliyun_client.{api}`），失败 RecordError+SetStatus
- Metrics：`aliyndns_operations_total{operation,status}`、`aliyndns_operation_duration_seconds{operation}`、`aliyndns_api_duration_seconds{api}`、`aliyndns_errors_total{error_type}`、`aliyndns_accounts{state}`——低基数 label，无 account_id 入 metric
- **敏感防线**：AccessKey/Secret/token 明文禁入日志与 label；httpx logger 提 WARNING——SDK RPC 请求 URL query 含 `AccessKeyId`，防 URL 日志泄漏

### 8.2 错误处理

- 授权失败 → `ToolError("permission denied on account {account_id} for {mode}")`，error_type=`no_permission`
- 账户未托管（`aliyndns:accounts:index` 查无）→ error_type=`account_not_found`；账户禁用（`enabled=false` 或凭证失效被标禁）→ error_type=`account_disabled`
- token 无效（`tokens:{hash}` 查无）→ error_type=`invalid_token`
- Alidns 错误按 §7.1 分类，含 `request_id` 供阿里云侧排查

### 8.3 测试

- `auth.py`：token 验证、账户级 read/write 判定（含 write⇒read 不变式、未授权账户拒绝）
- `account_store.py`：启动加载、Pub/Sub 热更新、断线重建订阅（redis-py ≥6 坑）
- 工具层：mock AlidnsClient（zabbix 模式），正常路径 + 错误分类 + 越权拒绝
- `tests/test_logging.py`：凭证/token 明文不进日志（回归防线，serpapi 模式）

## 9. 风险与兜底

| 风险 | 兜底 |
|---|---|
| FastMCP proxy header 转发行为依赖 `get_http_headers()` 默认不含 authorization 排除项；未来版本若排除 authorization，透传失效 | 若验证失效：gateway 加自定义 `StreamableHttpTransport` 子类（重写 header 合并，`include_all=True`），把 Authorization 显式转发——改动小、机制已确认存在 |
| 透传含 authorization 头到后端 | 内网部署（不映射宿主端口），且 MCP 自己验证 token 不盲信（防御纵深） |
| `tokens:{hash}` schema 耦合（MCP 依赖 gateway token 存储格式） | 同一 Redis、schema 稳定；MCP 只读 `id`/`name` 字段，变化时 gateway-admin 同步迁移 |
| 凭证探活消耗 | 查询免费（DescribeDomains），无配额担忧 |

## 10. 交付范围（本期一起做）

1. aliyun-dns-mcp：6 tools + 鉴权 + 凭证热更新 + 测试 + 文档（CLAUDE.md/README/RELEASE）
2. gateway-admin：账户管理页 + token×账户授权矩阵页
3. 端口登记：9054（根 CLAUDE.md 端口表 + compose）
4. gateway-proxy：零改动（若 §9 风险触发再改）
5. uv.lock 用阿里云镜像重建（模板强制）

## 11. 部署

- 容器内 9054，不映射宿主；compose 加 aliyun-dns-mcp 服务
- REDIS_URL 指向容器内 redis
- 生产构建：`uv sync --frozen --no-dev`，uv.lock 阿里云镜像（模板强制）
