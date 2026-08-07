# MCP 账户级细粒度权限模式（mcp-account-level-permission-pattern）

> 来源：aliyun-dns-mcp 开发与生产实践（2026-08）。
> 适用：MCP Gateway 下的 MCP 需要「比 server 更细」的权限粒度——同一 MCP 管理多个外部账户/租户/资源组，不同 token 只能访问其中一部分。

## 为什么需要

Gateway 的 token 权限模型是 `{server: {read, write}}`——粒度到 server。但有些 MCP 天然是多租户的：

- 阿里云 DNS：多个阿里云账户，每个账户多个域名
- 未来的多租户 CRM / 多项目 CI / 多账号云资源

需求：**token → 可访问的账户子集** + **账户级 read/write**（同一 token 对账户 A 只读、对账户 B 可写）。server 级权限太粗，无法表达。

## 核心设计

### 1. 关键前提：gateway 零改动——Authorization 自动透传

FastMCP 的 proxy transport（`StreamableHttpTransport`）在代理场景下**自动把当前 HTTP 请求的 headers 转发给后端**：

```python
# fastmcp/client/transports.py（源码确认）
client_kwargs["headers"] = get_http_headers() | self.headers
```

且 `get_http_headers()` 的默认排除列表**不含 authorization**（只排除 host/content-length/connection 等 hop-by-hop 头）。**因此 gateway 转发调用时，后端 MCP 已经收到调用方的 `Authorization: Bearer <token>`——无需任何 gateway 改动。**

> ⚠️ 风险：此行为依赖 FastMCP 实现细节。若未来版本排除 authorization，兜底方案是 gateway 加自定义 `StreamableHttpTransport` 子类显式转发（机制已确认存在，改动小）。

### 2. 权限分层：gateway 粗闸 + MCP 权威

| 层 | 粒度 | 职责 | 实现 |
|---|---|---|---|
| gateway-proxy（零改动） | server 级 read/write | 工具可见性粗闸——token 能否看到/调用写工具 | 现有 `check_permission`；授权矩阵保存时按 union 同步 `tokens:{hash}` 的 server read/write |
| 后端 MCP（权威） | **账户级 read/write** | 每次调用校验：token → 授权映射 → 账户存在且有所需 mode | MCP 读转发来的 Authorization → 验证 token → 查账户级授权映射 |

**为什么 MCP 要重复验证 token**：防御纵深——MCP 自己验证（SHA-256 + Redis `tokens:{hash}`，与 gateway 同套存储）拿到 token_id，再查账户级映射。绕过 gateway 直连（部署禁止，容器不映射宿主）也会被拒。

### 3. Redis 数据模型

```
<prefix>:accounts:{account_id}           Hash — 外部账户凭证
  access_key_id / access_key_secret / description / region / enabled / created_at / probe_error
  # 明文只存内网 Redis 值与内存，禁入日志/metric（只用 account_id）

<prefix>:accounts:index                  Set — 全部 account_id（管理页遍历）

<prefix>:token_accounts:{token_id}       Hash — 账户级授权（权威）
  field: {account_id} → JSON {"read": bool, "write": bool}
  不变式：write ⇒ read（要改记录必须能查记录；UI/API 强制 + MCP 防御式判定 read = read or write）

<prefix>:changed                          Pub/Sub — 账户/授权变更通知（MCP 全量重载触发）
  {"action": "upsert"|"delete", "key": "<完整 Redis key 含前缀>"}
```

要点：
- **account_id 人类可读**（`prod-main`），是 Redis key、授权字段、工具参数三合一
- **令牌-账户映射按 token_id 存**（非 token hash）：id 稳定可读；MCP 先读 `tokens:{hash}` 拿 id（顺带校验 token 有效性）
- **write ⇒ read 不变式双层防御**：UI 强制（write=true ⇒ read=true）+ MCP 防御式判定（read = `read or write`，防 Redis 手改出违规数据）

### 4. 授权矩阵 + union 同步（gateway-admin）

管理面新增「token × 账户 read/write」授权矩阵（勾选式，write 自动连带 read）。保存时：

1. 写 `<prefix>:token_accounts:{token_id}`（账户级授权权威）
2. **计算 union 同步 gateway token**：`tokens:{hash}` 的 `permissions["<server-name>"] = {read: any_read, write: any_write}`——保证有任一账户写权限的 token 能看到写工具（gateway 粗闸）
3. PUBLISH `<prefix>:changed` 热更新

三处必须一致维护 union：`put_perms`（保存矩阵）、`delete_account`（删账户清理引用 + 重算受影响 token 的 union）、`delete_token`（删 token 清授权）。提取公共 helper（`_recompute_union`）防三处漂移。

### 5. MCP 侧执行（每调用）

```
工具调用 → 读 get_http_headers(include_all=True) 取 authorization
  → SHA-256 → tokens:{hash} → token_id（查无 = invalid_token 拒绝）
  → <prefix>:token_accounts:{token_id} → {account_id: {read, write}}
  → 校验 account_id ∈ 映射 且 perm[required_mode]（read 判定 = read or write）
  → 通过后用该账户凭证调外部 API
```

鉴权失败抛 `ToolError("permission denied: {error_type}: {message}")`——消息可含 account_id（非敏感），token/密钥永不进错误消息。

## 可复用组件

- **AccountStore**（aliyun-dns-mcp/account_store.py）：Redis 账户 + 授权加载、Pub/Sub 热更新（含断线重建订阅）、懒加载 token 权限缓存
- **PermissionChecker**（aliyun-dns-mcp/auth.py）：token 验证 + 账户级 read/write 判定
- **授权矩阵 API**（gateway-admin/api/aliyun_perms.py）：矩阵读写 + union 同步
- 新多租户 MCP：复制这三件 + 改前缀（`<prefix>:`）与 server 名

## 与 key 池模式的区别

| | key 池（search-mcp-key-pool-pattern） | 账户级权限（本文） |
|---|---|---|
| 解决 | 多 key 轮换/配额/失效剔除 | 多账户授权边界（谁能碰哪个账户） |
| 粒度 | key 之间等价可替换 | 账户之间隔离不可替 |
| 授权 | 无（key 即凭证） | token → 账户 read/write 双维度 |
| 存储 | `search:keys:<provider>` | `<prefix>:accounts:*` + `<prefix>:token_accounts:*` |

两者可组合：账户级权限管"谁能用"，key 池管"用哪个 key"。
