# Aliyun DNS MCP — 发布指南

## 版本管理

遵循 [SemVer](https://semver.org/)：
- `MAJOR`: breaking changes（tool 签名变更、删除 tool）
- `MINOR`: 新增功能（新 tool、新 resource）
- `PATCH`: bug fix、文档更新

## 发布流程

### 1. 本地验证

```bash
# 跑全量测试（注意：必须 python -m pytest，管道 tail 可能挂）
uv run python -m pytest tests/ -q

# 启动 server，冒烟验证
redis-server --daemonize yes
REDIS_URL=redis://localhost:6379/0 uv run python server.py
# 预期日志: account_store_loaded + otel_metrics_configured service=aliyun-dns-mcp port=9464

# 检查 protocol 兼容性
curl -s -X POST http://127.0.0.1:9054/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"check","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}' | python3 -m json.tool
# 预期 tools 6 个: list_accounts / list_domains / list_records / add_record / update_record / delete_record
```

### 2. 更新版本

```bash
# 更新 pyproject.toml 中的 version
# 更新 README.md changelog
```

### 3. 构建 & 发布

本 MCP 为内部 server（不发布 PyPI），部署走 Docker 镜像：

```bash
docker build -t aliyun-dns-mcp:<version> .
docker push <registry>/aliyun-dns-mcp:<version>
```

### 4. 发布后检查

- [ ] 镜像构建成功（uv.lock 必须阿里云镜像源，见下）
- [ ] gateway-admin 注册的 server 探活正常
- [ ] 账户授权矩阵保存后热更新生效（无需重启 MCP）
- [ ] 全量测试通过

## 部署到正式环境

### 环境要求

- Python >=3.12
- Redis（10.33.17.72 内网已有实例）
- Docker / K8s

### ⚠️ uv.lock 必须用阿里云镜像（生产构建前提）

生产服务器**无法访问 files.pythonhosted.org**。uv.lock 里 URL 写死官方源会导致生产构建失败。**新建/改依赖后必须重建 lock 指向阿里云**：

```bash
rm -f uv.lock
UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock
# 验证: grep -c mirrors.aliyun.com uv.lock 应 >0; files.pythonhosted.org 应为 0
```

### Docker 部署

```dockerfile
FROM python:3.12-slim
# healthcheck 用 curl（slim 镜像无 curl，见「compose 示例」）
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY server.py aliyun_client.py auth.py account_store.py logging_config.py telemetry.py ./
COPY tools/ ./tools/
CMD ["uv", "run", "python", "server.py"]
```

### compose（并入 gateway compose）

```yaml
aliyun-dns-mcp:
  image: <registry>/aliyun-dns-mcp:<version>
  environment:
    REDIS_URL: redis://redis:6379/0
    MCP_PORT: "9054"            # 容器内固定；如需宿主映射（不建议），改 ports
    PROMETHEUS_PORT: "9464"     # 容器内固定
    LOG_FORMAT: json
    OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
  networks: [gateway-net]       # 与 gateway-proxy 同网，供其内网访问
  depends_on: [redis]
  # 如确需宿主访问 metrics，加 ports（错开宿主端口）：
  # ports: ["9469:9464"]
  healthcheck:
    test: ["CMD", "curl", "-f", "-X", "POST", "http://127.0.0.1:9054/mcp",
           "-H", "MCP-Protocol-Version: 2026-07-28", "-H", "Mcp-Method: tools/list",
           "-d", "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{\"_meta\":{\"io.modelcontextprotocol/protocolVersion\":\"2026-07-28\",\"io.modelcontextprotocol/clientInfo\":{\"name\":\"health\",\"version\":\"1.0\"},\"io.modelcontextprotocol/clientCapabilities\":{}}}}"]
    interval: 30s
    timeout: 5s
    retries: 3
```

### 部署步骤（10.33.17.72）

1. 本地：`UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock` 重建 lock，`docker build -t aliyun-dns-mcp:<version> .`，push 到私有 registry
2. 服务器：拉镜像，更新 compose 中 version
3. gateway-admin → Servers：注册 `aliyun-dns-mcp`（URL `http://aliyun-dns-mcp:9054/mcp`）
4. gateway-admin → Tokens：为 token 勾选 `aliyun-dns-mcp` read/write
5. gateway-admin → 账户授权：为 token 配置账户级 read/write（Redis 写 + `aliyndns:changed` pubsub 热更新，MCP 无需重启）

### 健康检查

```bash
curl -X POST http://<host>:9054/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"health","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}'
```

### 可观测性

- 结构化日志 → stdout（key=value 格式，`LOG_FORMAT=json` 切 JSON 对接 Loki/ELK）
- Prometheus metrics → 容器内 `:9464/metrics`（如需宿主访问，ports 错开映射，见 compose 示例）
- OpenTelemetry traces → `OTEL_EXPORTER_OTLP_ENDPOINT`（未设时 console span）

## 已知注意事项

- **network_error 兜底**（spec §7.1）：工具层只捕获 `AlidnsError`。`AliyunClient._call` 的 `except Exception` 已把所有 SDK 异常（含网络/超时）包成 `AlidnsError`（不匹配分类时落 `api_error`），网络错误不会 500，但 error_type 非精确 `network_error`；Redis 连接异常与意外 bug 冒泡由 FastMCP 转 is_error。小规模 + 内网部署风险低，保持现状
- **httpx WARNING 防线**：httpx logger 提到 WARNING（SDK RPC URL query 含 AccessKeyId）。防线在 `logging_config.configure_logging`，任何不走它的进程会失去防线

## 端到端验证

- 验证日期：2026-08-06
- 环境：本地 macOS，redis 6379 + aliyun-dns-mcp:9054 + gateway-proxy:8082 + gateway-admin:8081 四服务（无真实阿里云凭证，测试账户 AccessKey 用 `LTAI-test`/`sk-test`，验证到"阿里云 API 拒绝"即证明链路通）
- 配置路径：管理 API 登录（admin/admin123）→ 注册 server（url `http://localhost:9054/mcp`）→ 添加账户 `test-acct`（probe:false）→ 创建 token `dns-ro`（server 级 read）→ 配授权矩阵（test-acct read:true write:false）

### 关键验证 1：Authorization 头经 gateway 透传到 MCP ✅ 通过（完整链路）

| 场景 | 命令 | 结果 |
|---|---|---|
| 不带 token 经 gateway 调 `list_accounts` | `curl -X POST localhost:8082/mcp -d '{"method":"tools/call",...}'`（无 Authorization） | `Permission denied: invalid_token` ✅ |
| 带 token 经 gateway 调 `list_accounts` | 同上 + `Authorization: Bearer $TOK` | 返回 `[{"account_id":"test-acct","read":true,"write":false}]` ✅ |

**结论：Authorization 头由 gateway-proxy 原样透传到 aliyun-dns-mcp，MCP 侧重新验证 token 成功**（返回账户数据而非权限错误）——spec §6.3「gateway 零改动」假设成立，无需走 §9 回退方案。带 token 后 MCP 内 `get_http_headers(include_all=True)` 路径被真实请求覆盖。

### 关键验证 2：账户级读写 ✅ 通过（含热更新）

| 场景 | 结果 |
|---|---|
| read 权限 token 调 `delete_record`（write 工具） | MCP 拒绝 `permission_denied` ✅（账户级校验生效，未经 gateway 粗闸放行到 API） |
| 授权矩阵改 test-acct write 后（PUT /api/aliyun-perms，pubsub 热更新）调 `delete_record` | 穿透鉴权，到达真实阿里云 API：`error_type=invalid_credential`、`InvalidAccessKeyId.NotFound`（假凭证被 API 拒）✅ |
| 同上 write 后调 `list_domains`（read 工具） | 同样到达 API 层 `invalid_credential` ✅ |

### 实测发现并修复的缺陷（SDK 调用层）

- **`with_options` 缺 runtime 参数**：真实 SDK 的 `delete_domain_record_with_options(request, runtime)` 第二个参数**必传**，此前 `_call` 只传 request → 假凭证调用时（唯一能触达 SDK 真实调用的场景）抛 `TypeError: missing 1 required positional argument: 'runtime'`，被 classify_error 兜底成 `api_error`。修复：`AlidnsClient.__init__` 构造 `RuntimeOptions()`（`darabonba.runtime`），`_call` 统一注入 `fn(request, self._runtime)`；`tests/test_aliyun_client.py` fake 签名改为 `(request, runtime)` 同构 + 新增 `test_with_options_gets_runtime` 回归，`tests/test_metrics.py` stub 同步。**该缺陷在单测里无法暴露**（fake 签名只有 request），端到端验证是唯一能抓到它的环节
- 顺带验证：`InvalidAccessKeyId.NotFound` 被 `classify_error` 正确分类为 `invalid_credential`（Task 4 错误分类实测补全）

### 局限

- 未使用真实阿里云凭证，未验证真实域名操作（add/update/delete 到 API 层为止）；后续接真实账户时按「部署步骤」配好 AccessKey 后冒烟
- gateway 审计写 MySQL 未在本机验证（本地无 MySQL，MYSQL_URL 未配，懒加载不触发）；容器部署由 compose 内置

## Changelog

<!-- 每次发布追加 -->

### Unreleased

- 初始版本：server 装配 + 6 tools + 账户级权限 + Redis 热更新 + telemetry/logging + 文档三件套
- fix：`with_options` 补 runtime 参数（端到端验证实测 TypeError，`_call` 统一注入 `RuntimeOptions()`）
