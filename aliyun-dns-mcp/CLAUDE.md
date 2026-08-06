# Aliyun DNS MCP — 开发说明

## 概述

阿里云 DNS 解析管理 MCP server：托管多个阿里云账户，按账户查询/管理 DNS 解析记录。接入 MCP Gateway，权限支持两个维度（账户维度 + 账户级读写维度），**MCP 是账户级权限的权威**（gateway 零改动）。

## 架构

```
MCP Client
  └─ Bearer <gateway_token> ─→ gateway-proxy:8082
                                ├─ verify_token（SHA-256 + Redis tokens:{hash}）
                                ├─ 工具可见性粗闸（server 级 read/write，来自 union）
                                └─ proxy 自动转发（含 Authorization 头）
                                            └─→ aliyun-dns-mcp:9054
                                                  ├─ 读 Authorization → hash → tokens:{hash} → token_id
                                                  ├─ 查 aliyndns:token_accounts:{token_id} → 账户级 read/write
                                                  ├─ 校验 account_id ∈ 授权映射 + 所需 mode
                                                  ├─ 用该账户 AccessKey 调阿里云 Alidns API（SDK）
                                                  └─ Redis 账户凭证缓存 + Pub/Sub 热更新
```

- FastMCP v4（`fastmcp==4.0.0b1`）+ MCP Protocol 2026-07-28（stateless HTTP）
- `aliyun_client.py`：官方 SDK（alibabacloud-alidns20150109）封装，同步调用走 `asyncio.to_thread`，错误分类 + OTel span
- `account_store.py`：Redis 账户凭证 + token 权限缓存 + Pub/Sub 热更新
- `auth.py`：token 验证（与 gateway 同一套 `tokens:{hash}`）+ 账户级 read/write 校验
- `tools/`：6 个模块级工具函数（可测试）+ 显式具名注册
- 6 个 tool：list_accounts / list_domains / list_records（read）+ add_record / update_record / delete_record（write）

## 权限模型（双维度，MCP 为权威）

| 维度 | 位置 | 机制 |
|---|---|---|
| 账户级 read/write（权威） | 本 MCP（auth.py） | 每次调用：hash Authorization → `tokens:{hash}` → token_id → `aliyndns:token_accounts:{token_id}` → 校验账户存在且 `perm[required_mode]`，否则 ToolError 明确报"无权限" |
| 工具可见性粗闸 | gateway-proxy（零改动） | 授权矩阵保存时按 union 同步 `tokens:{hash}` 的 `aliyun-dns-mcp` read/write |

- 不变式：write ⇒ read（UI/API 保存时强制；本 MCP 侧防御式判定 read = `read or write`，防 Redis 手改出违规数据）
- MCP 重复验证 token 是**防御纵深**：绕过 gateway 直连（部署禁止，容器不映射宿主）也会被拒
- 错误消息可含 account_id（非敏感）；token/密钥永远不进错误消息

## Redis schema（三件套）

```
aliyndns:accounts:{account_id}          Hash — 阿里云账户凭证
  access_key_id / access_key_secret     # 明文只存内网 Redis 值与内存，禁入日志/metric
  description / region / enabled / created_at / probe_error

aliyndns:accounts:index                 Set — 全部 account_id（管理页遍历）

aliyndns:token_accounts:{token_id}      Hash — 账户级授权（权威）
  field: {account_id} → JSON {"read": bool, "write": bool}

aliyndns:changed                        Pub/Sub — 账户/授权变更通知（全量重载触发）
```

- account_id 命名人类可读（`prod-main`），是 Redis key、授权字段、tool 参数三合一
- 令牌-账户映射按 token_id 存（非 token hash）：MCP 先读 `tokens:{hash}` 拿 id（顺带校验 token 有效性）
- 热更新：gateway-admin 写 Redis 后 PUBLISH `aliyndns:changed`，AccountStore 监听后全量重载（小规模，成本可忽略）

## 工具

| Tool | 模式 | 参数 | Alidns API | 说明 |
|---|---|---|---|---|
| `list_accounts` | read | - | 无 | 当前 token 可访问账户及其读写权限 |
| `list_domains` | read | `account_id` | DescribeDomains | 域名列表（前 100） |
| `list_records` | read | `account_id`, `domain_name` | DescribeDomainRecords | 解析记录列表（前 100） |
| `add_record` | write | `account_id`, `domain_name`, `rr`, `type`, `value`, `ttl`(600), `priority` | AddDomainRecord | ⚠️ 写操作 |
| `update_record` | write | `account_id`, `record_id`, `rr?`, `type?`, `value?`, `ttl?`, `priority?` | UpdateDomainRecord | ⚠️ 写操作，至少一个更新字段 |
| `delete_record` | write | `account_id`, `record_id` | DeleteDomainRecord | ⚠️ 写操作，不可撤销 |

返回结构：`{"status": "ok"|"error", "data": ..., "error_type": ..., "message": ..., "request_id": ...}`；鉴权失败抛 ToolError（`permission denied: {error_type}: {message}`）。

错误分类（aliyun_client.py classify_error）：`invalid_credential` / `throttled` / `not_found` / `api_error`（SDK 异常全包成 AlidnsError，网络错误落到 api_error，见「已知注意事项」）。

## 依赖与安装（必须）

- FastMCP v4（`fastmcp==4.0.0b1`）+ MCP Protocol `2026-07-28`（stateless HTTP）
- Python >=3.12，包管理 uv（`--prerelease=allow`）

```bash
# 安装依赖（必须 --all-extras，否则 venv 无 pytest）
uv sync --all-extras

# 跑测试（用 python -m pytest，管道 tail 可能挂）
uv run python -m pytest tests/ -q
```

### ⚠️ uv.lock 必须用阿里云镜像（生产构建前提）

生产服务器**无法访问 files.pythonhosted.org**。uv.lock 里 URL 写死官方源会导致生产构建失败。**新建/改依赖后必须重建 lock 指向阿里云**：

```bash
rm -f uv.lock
UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock
# 验证: grep -c mirrors.aliyun.com uv.lock 应 >0; files.pythonhosted.org 应为 0
```

Dockerfile 用 `uv sync --frozen --no-dev`（lock 决定依赖）。

## 端口

- **MCP 容器内 9054**（根 CLAUDE.md 端口表登记），不映射宿主
- PROMETHEUS_PORT 容器内 9464（compose 宿主端错开，如 9469）

## 可观测性

### Traces
- FastMCP 自动为每个 MCP 操作创建 span；AlidnsClient 每 API 调用一个 span（`aliyun_client.{api}`），失败 RecordError+SetStatus
- `OTEL_EXPORTER_OTLP_ENDPOINT` 未设时 console span（开发）

### Metrics（Prometheus，`http://localhost:9464/metrics`）
- `aliyndns_requests_total` — MCP tool 调用总数
- `aliyndns_request_duration_seconds` — MCP tool 调用延迟
- `aliyndns_errors_total` — MCP tool 错误数
- `aliyndns_dependency_duration_seconds` — Alidns API 调用延迟
- `aliyndns_dependency_errors_total` — Alidns API 错误数
- `aliyndns_in_flight_requests` — 处理中请求数

### Logs
- structlog 结构化 key=value；`LOG_FORMAT=json` 切 JSON（Loki/ELK）
- 所有日志带 `service="aliyun-dns-mcp"`；错误带 `error` key

## 踩坑记录（为什么/注意点）

- **`get_http_headers` 必须 `include_all=True`**（auth.py）：默认版会排除 `authorization` 头——漏掉则 token 校验恒失败（gateway 注释同款坑，tests/test_auth.py 有 monkeypatch 回归）
- **pubsub 连接死后不自动重连**（account_store.py）：except 分支必须 `aclose()` 旧的 → 重新 `pubsub()` → 重新 subscribe，否则热更新永久失效只能重启进程
- **httpx logger 提 WARNING**（logging_config.py）：阿里云 SDK RPC 请求 URL query 含 AccessKeyId，httpx 默认 INFO 打印完整 URL 会泄漏凭证。`tests/test_logging.py` 是回归防线。这行是必守项
- **SDK 同步走 `asyncio.to_thread`**（aliyun_client.py）：SDK 是同步 API，直接调用会阻塞 event loop
- **`with_options` 第二个参数 runtime 必传**（aliyun_client.py）：真实 SDK 方法签名 `(request, runtime)`，缺它直接 TypeError（端到端验证实测，单测 fake 只签 request 掩盖了它）——`_call` 统一注入 `AlidnsClient.__init__` 构造的 `RuntimeOptions()`（`darabonba.runtime`），fake 签名必须保持同构
- **pubsub listener 与 server 同 event loop**（server.py `_run`）：跨 loop 用 redis 连接直接 RuntimeError（serpapi 教训）
- **FastMCP v4 拒绝 `*args/**kwargs` 工具包装**：register() 显式具名包装（tools/__init__.py）
- **stateless 模式 lifespan 不可靠**：进程级模块单例 init（server.py `_init_runtime`）
- **指标必须运行时取值**（tools/__init__.py 与 aliyun_client.py 头部 CRITICAL 注释）：`from telemetry import X` 在模块加载瞬间（init_telemetry 之前）把指标绑定为 None，之后 init_telemetry 只更新 telemetry 模块自身——4 个 tool 级指标与 2 个 dependency 指标将静默失效。必须 `import telemetry` 后运行时访问 `telemetry.REQUESTS_TOTAL` 等（`tests/test_metrics.py` monkeypatch 回归验证）
- **ToolAnnotations 字段名**：读取端用 snake_case（`destructive_hint`/`read_only_hint`）——驼峰（`destructiveHint`）是 pydantic alias，读取触发 FastMCPDeprecationWarning（v4.0.0b1 实测）；**写入端**（`ToolAnnotations(destructiveHint=True)`）两种都兼容，保持驼峰与 gateway 读取一致
- redis-py ≥6 `get_message()` 不传 `ignore_subscribe=True`（参数已改名，必 TypeError 静默失效）——用 `type=="message"` 过滤最稳

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `REDIS_URL` | 无（必填） | Redis（账户凭证 + 权限 + 热更新 pubsub） |
| `MCP_HOST` | `0.0.0.0` | 监听地址 |
| `MCP_PORT` | `9054` | MCP 端口（根 CLAUDE.md 登记） |
| `LOG_FORMAT` | `console` | `console`（开发）/ `json`（生产） |
| `PROMETHEUS_PORT` | `9464` | Prometheus /metrics 端口 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 无 | OTLP collector（未设则 console span） |
| `OTEL_SERVICE_NAME` | `aliyun-dns-mcp` | 服务名（trace/metrics label） |
| `LOG_FILE` | 无 | 日志文件路径（RotatingFile 10MB x 5） |

## 本地开发

```bash
uv sync --all-extras
redis-server --daemonize yes   # 需要本地 Redis（启动冒烟/热更新）
REDIS_URL=redis://localhost:6379/0 uv run python server.py   # 起服务
uv run python -m pytest tests/ -q   # 全量测试
```

## 已知注意事项

- **network_error 兜底**（Task 7 决策，spec §7.1）：工具层只捕获 `AlidnsError`，其余异常冒泡。实测 `AlidnsClient._call` 的 `except Exception` 已把所有 SDK 异常（含网络/超时）包成 `AlidnsError`（classify_error 不匹配时落到 `api_error`），**网络错误不会 500**，但 error_type 不是精确的 `network_error`。剩余冒泡路径：Redis 连接异常（checker 内）与意外 bug——FastMCP 转 is_error 响应，client 可读。小规模 + 内网部署风险低，保持现状；若需精确分类，改 `classify_error` 加网络异常判定即可
- **httpx WARNING 防线**：logging_config 是唯一入口，任何绕过（直接调 SDK 不经 configure_logging 的进程）会失去防线——测试进程与生产进程都走 server.py 的 `_configure_logging()`

## 代码注释规范

遵循 OBS-CORE-005：注释写"为什么"不写"做了什么"。

## 知识库

开发时查阅 `../knowledge-base/fastmcp-v4/` — FastMCP v4 完整文档。
