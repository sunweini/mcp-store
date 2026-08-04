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
├── gateway-proxy/           # MCP 网关代理（FastMCP 4.0）
├── gateway-admin/           # 网关管理界面（FastAPI + Vue 3）
└── <mcp-name>/              # 每个 MCP 独立目录
    ├── CLAUDE.md            # MCP 级开发说明
    ├── README.md            # 功能说明（给用户看）
    ├── RELEASE.md           # 发布指南
    ├── server.py            # Server 入口
    ├── client.py            # 测试 client（可选）
    ├── pyproject.toml       # 依赖管理（uv）
    └── tests/               # 测试
```

## 架构概览

### MCP Gateway 架构

```
MCP Client -> gateway-proxy:8082 -> [zabbix-mcp:9053, tavily-mcp:9050, ...]
                  ↑
            gateway-admin:8081 (管理界面)
                  ↑
        ┌─────────┴─────────┐
        ↓                   ↓
    Redis（配置/状态）    MySQL（调用审计）
    - servers 注册        - calls 表（全量 tools/call）
    - tokens              - 聚合统计源（重启不丢）
    - key 池（search:keys）
    - audit:failures（失败流）
```

**两个核心服务：**
- `gateway-proxy`：MCP 协议代理，Token 验证，读写权限控制，调用审计写 MySQL
- `gateway-admin`：管理 API + Vue 3 前端（Server/Token/API Keys 管理、监控面板、请求日志）

**两个存储：**
- `Redis`：配置与状态（server 注册、token、key 池、失败审计流）--热数据低延迟
- `MySQL`：调用审计日志（calls 表，全量 tools/call）--聚合统计与明细，持久化重启不丢

### 接入 Gateway 流程

开发新 MCP 时，需确保：

1. **Server 命名与描述**
   - name 用小写字母/数字/连字符，**禁止下划线**（namespace 前缀用 `_` 切分）
   - 写清 server 描述 + 每个 tool 的 docstring（管理界面展示，配权限参考）
   - 写操作 tool docstring 含 `⚠️ 写操作` 标记

2. **Tool 标注 annotations**（读写分离）
   ```python
   @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
   def read_op(...): ...
   
   @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
   def write_op(...): ...
   ```
   判定：`destructiveHint=True` → write，否则 read。漏标当 read。

3. **健康探活** — MCP 标准 `ping`，FastMCP 原生支持，无需额外开发。Gateway 每 30s 探活。

4. **在管理界面注册 MCP server**
   - 访问 `http://localhost:8081`
   - 添加 server：name + URL + description
   - 注册时自动拉 `tools/list`（识别读/写）+ 自动探活

5. **创建 API Token 并配置权限**
   - 选择可访问的 MCP server
   - 勾选 read/write 权限
   - Token 明文只显示一次（存哈希）

6. **MCP Client 连接配置**
   ```json
   {
     "mcpServers": {
       "gateway": {
         "url": "http://localhost:8080/mcp",
         "headers": {
           "Authorization": "Bearer <token>"
         }
       }
     }
   }
   ```

## 开发偏好

- **语言**: 中文对话，技术术语保留英文
- **框架**: FastMCP v4（`fastmcp==4.0.0b1`）+ MCP Protocol `2026-07-28`
- **包管理**: uv（`--prerelease=allow` 因 FastMCP v4 是 beta）
- **Python**: >=3.12
- **可观测性**: 结构化日志 + OpenTelemetry（遵循 `~/.claude/docs/observability-coding-standards.md`）
- **传输协议**: Streamable HTTP，stateless 模式优先
- **代码风格**: 注释写"为什么"不写"做了什么"

## 端口规范

**MCP server 容器内端口统一分配 9050-9500**，新增 MCP 前先在此登记。存储服务端口单独列。

| 端口 | 服务 | 说明 |
|---|---|---|
| 9050 | tavily-mcp | 搜索源（5 tools） |
| 9051 | brave-mcp | 搜索源（2 tools） |
| 9052 | serpapi-mcp | 搜索源（5 engines） |
| 9053 | zabbix-mcp | 告警巡检（8 tools） |
| 6379 | redis | 配置/状态/失败审计（容器内，不映射宿主） |
| 3306 | mysql | 调用审计 calls 表（容器内，不映射宿主） |

- MCP server / redis / mysql 容器内端口**不映射宿主端口**（与 gateway-proxy 8082 / gateway-admin 8081 分离，减少攻击面）
- 新增 MCP：从 9050-9500 取最小未用端口，登记本表 + 更新 compose
- 本地非容器开发时按表使用对应本地端口

## 已开发 MCP

| 目录 | 名称 | 说明 | 状态 |
|---|---|---|---|
| `zabbix-mcp/` | Zabbix MCP | Zabbix 告警巡检/维护期/告警确认（8 tools） | ✅ 开发完成 |
| `tavily-mcp/` | Tavily MCP | Tavily 搜索（search/extract/crawl/map/research 5 tools） | ✅ 开发完成 |
| `brave-mcp/` | Brave MCP | Brave 搜索（web/local 2 tools） | ✅ 开发完成 |
| `serpapi-mcp/` | SerpAPI MCP | SerpAPI 搜索（google/bing/baidu/duckduckgo/ebay 5 engines） | ✅ 开发完成 |

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

## MCP 开发规范（Gateway-ready 强制项）

每个 MCP 必须满足以下规范才能接入 Gateway。详细写法见 `templates/mcp-template/CLAUDE.md`。

| 规范 | 要求 | 说明 |
|---|---|---|
| **Server 命名** | 小写字母/数字/连字符，禁下划线 | namespace 前缀用 `_` 切分，含下划线致路由歧义 |
| **Server 描述** | 一句话说清能力 | 注册时填，管理界面展示 |
| **Tool 描述** | docstring 写清用途 | Gateway 拉取展示，配权限参考 |
| **读写分离** | 全部 tool 标 annotations | `destructiveHint=True` → write，否则 read |
| **写操作标记** | docstring 含 `⚠️ 写操作` | AI 读到此标记走用户确认流程 |
| **健康探活** | 支持 MCP `ping` | FastMCP 原生支持，无需额外开发 |
| **可观测性** | structlog + OTel | 遵循 `~/.claude/docs/observability-coding-standards.md` |
| **代码注释** | 写"为什么"不写"做了什么" | OBS-CORE-005 |

