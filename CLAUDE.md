# MCP Store — 多 MCP 开发仓库

## 仓库约定

每个 MCP server 独立目录，独立依赖，独立发布。

```
mcpstore/
├── CLAUDE.md                # 本文件 — 仓库级说明
├── .claude/                 # Claude Code 配置
├── knowledge-base/          # 开发知识库
│   └── fastmcp-v4/          # FastMCP v4 官方文档（38 篇）
├── templates/
│   └── mcp-template/        # 新建 MCP 时复制此目录
└── <mcp-name>/              # 每个 MCP 独立目录
    ├── CLAUDE.md            # MCP 级开发说明
    ├── README.md            # 功能说明（给用户看）
    ├── RELEASE.md           # 发布指南
    ├── server.py            # Server 入口
    ├── client.py            # 测试 client（可选）
    ├── pyproject.toml       # 依赖管理（uv）
    └── tests/               # 测试
```

## 开发偏好

- **语言**: 中文对话，技术术语保留英文
- **框架**: FastMCP v4（`fastmcp==4.0.0b1`）+ MCP Protocol `2026-07-28`
- **包管理**: uv（`--prerelease=allow` 因 FastMCP v4 是 beta）
- **Python**: >=3.12
- **可观测性**: 结构化日志 + OpenTelemetry（遵循 `~/.claude/docs/observability-coding-standards.md`）
- **传输协议**: Streamable HTTP，stateless 模式优先
- **代码风格**: 注释写"为什么"不写"做了什么"

## 已开发 MCP

| 目录 | 名称 | 说明 | 状态 |
|---|---|---|---|
| `zabbix-mcp/` | Zabbix MCP | Zabbix 告警巡检/维护期/告警确认（8 tools） | ✅ 开发完成 |

## 知识库（开发必读）

`knowledge-base/fastmcp-v4/` 包含 FastMCP v4 官方文档 39 篇。

### ⚠️ 强制规则：写代码前必须先读知识库

以下场景**必须先 Read 对应知识库文件**，再写任何代码。不读不写。

| 触发场景 | 必读文件 | 说明 |
|---|---|---|
| 新建 MCP / 写 server.py | `10-server.md` → `11-tools.md` → `15-sessions.md` | server 基础 + tool 定义 + session |
| 定义/修改 Tool | `11-tools.md` | inputSchema/outputSchema/annotations |
| 定义/修改 Resource | `12-resources.md` | cache hints / template |
| 定义/修改 Prompt | `13-prompts.md` | 参数、arguments |
| 需要跨请求状态 | `15-sessions.md` | UserSession / SessionId |
| 后台长任务 | `16-tasks.md` + `22-client-tasks.md` | Tasks extension |
| 注册 Extension | `17-extensions.md` | add_extension() |
| 加 Middleware | `19-middleware.md` | 请求拦截链 |
| 写 Client 代码 | `20-client.md` → `21-transports.md` | client 基础 + transport |
| 处理 elicitation / MRTR | `60-server-elicitation.md` + `23-elicitation.md` | multi-round-trip |
| 加 OpenTelemetry / 日志 | `40-telemetry.md` + `42-logging.md` | 可观测性 |
| 写测试 | `41-testing.md` | 测试策略 |
| 加认证/授权 | `50-authorization.md` → `51-authentication.md` | auth 体系 |
| HTTP 部署 / Docker | `30-deployment-http.md` + `31-running-server.md` | 部署 |
| Server 配置 / 环境变量 | `32-server-configuration.md` | 配置项 |
| Server 组合 / mount | `18-composition.md` | 多 server 组合 |
| 加 MCP App（UI） | `70-apps-overview.md` → `71-apps-quickstart.md` | server-rendered UI |
| 从 v3 升级代码 | `03-upgrade-from-v3.md` | breaking changes |
| 不确定 API 用法 | `02-whats-new-v4.md` | v4 全特性概览 |

### 使用方式

```
# 开发时直接让我读：
"读 knowledge-base/fastmcp-v4/11-tools.md，然后帮我写一个 search tool"

# 或者描述场景，我会自动匹配：
"帮我加个后台任务" → 我会先读 16-tasks.md 再写代码
```

完整索引：`knowledge-base/fastmcp-v4/README.md`

## 新建 MCP 流程

```bash
# 1. 复制模板
cp -r templates/mcp-template <mcp-name>
cd <mcp-name>

# 2. 初始化 uv 项目
uv init --no-readme
# 编辑 pyproject.toml 加 fastmcp 依赖

# 3. 安装依赖
uv add "fastmcp==4.0.0b1" --prerelease=allow
# 确保 pyproject.toml 有 [tool.uv] prerelease = "allow"

# 4. 开发 server.py

# 5. 测试
uv run python server.py   # 启动
uv run python client.py   # 验证

# 6. 更新根 CLAUDE.md 的 MCP 列表
```

## MCP 目录规范

每个 MCP 目录必须包含：

| 文件 | 用途 | 必须 |
|---|---|---|
| `CLAUDE.md` | 开发说明、架构决策、注意事项 | ✅ |
| `README.md` | 功能说明、使用方法、配置项 | ✅ |
| `RELEASE.md` | 发布流程、版本管理、部署步骤 | ✅ |
| `server.py` | Server 入口 | ✅ |
| `pyproject.toml` | 依赖声明 | ✅ |
| `tests/` | 测试代码 | 推荐 |
| `client.py` | 测试用 client | 可选 |

## 协议版本

统一使用 MCP `2026-07-28` spec：
- Stateless HTTP transport（无 session）
- `Mcp-Method` + `Mcp-Name` routing headers
- `ttlMs` + `cacheScope` 缓存提示
- W3C Trace Context 传播
- JSON Schema 2020-12 output schema
- `_meta` 必携带 `protocolVersion` + `clientInfo` + `clientCapabilities`
