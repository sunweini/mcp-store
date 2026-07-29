# FastMCP v4 知识库索引

> FastMCP 4.0.0b1 | MCP Protocol 2026-07-28
> 来源: https://gofastmcp.com | 抓取日期: 2026-07-29

## 🚀 入门（必读）

| 文件 | 内容 | 优先级 |
|---|---|---|
| `01-quickstart.md` | 安装 + 第一个 server/client | ★★★ |
| `02-whats-new-v4.md` | v4 全部新特性概览 | ★★★ |
| `03-upgrade-from-v3.md` | v3→v4 迁移指南（breaking changes） | ★★★ |

## 🖥️ Server 开发

| 文件 | 内容 | 优先级 |
|---|---|---|
| `10-server.md` | FastMCP server 核心、创建、配置 | ★★★ |
| `11-tools.md` | Tool 定义、inputSchema/outputSchema、annotations | ★★★ |
| `12-resources.md` | Resource 定义、template、cache hints (SEP-2549) | ★★★ |
| `13-prompts.md` | Prompt 定义、参数、arguments | ★★☆ |
| `14-context.md` | Context 对象、请求级数据传递 | ★★★ |
| `15-sessions.md` | ⭐ Stateless session、UserSession、SessionId (SEP-2567) | ★★★ |
| `16-tasks.md` | ⭐ Background Tasks extension (SEP-2663) | ★★★ |
| `17-extensions.md` | ⭐ Extension 框架、add_extension() (SEP-2133) | ★★★ |
| `18-composition.md` | Server 组合、mount、proxy | ★★☆ |
| `19-middleware.md` | Middleware 链、请求拦截 | ★★☆ |

## 👤 Client 开发

| 文件 | 内容 |
|---|---|
| `20-client.md` | Client 基础、连接、协议协商 |
| `21-transports.md` | Transport 选择（stdio/HTTP/SSE） |
| `22-client-tasks.md` | Tasks client（tasks/get, tasks/update, tasks/cancel） |
| `23-elicitation.md` | Client 端 elicitation 处理 |

## 🚀 部署

| 文件 | 内容 |
|---|---|
| `30-deployment-http.md` | HTTP 部署（ASGI/Docker/反向代理/无状态） |
| `31-running-server.md` | 运行 server 各种方式 |
| `32-server-configuration.md` | Server 配置项、环境变量 |

## 📊 可观测性 & 测试

| 文件 | 内容 |
|---|---|
| `40-telemetry.md` | ⭐ OpenTelemetry 集成、span、metrics |
| `41-testing.md` | 测试策略、Client 测试模式 |
| `42-logging.md` | 结构化日志配置 |

## 🔐 认证 & 授权

| 文件 | 内容 |
|---|---|
| `50-authorization.md` | ⭐ 授权（scope, require_roles, step-up） |
| `51-authentication.md` | 认证概览、OAuth/OIDC |
| `52-oauth-proxy.md` | OAuth Proxy 完整实现 |
| `53-token-verification.md` | JWT/token 验证 |

## 🔧 高级特性

| 文件 | 内容 |
|---|---|
| `60-server-elicitation.md` | Server 端 elicitation、MRTR (SEP-2322) |
| `61-completions.md` | Argument 自动补全 |
| `62-dependency-injection.md` | 依赖注入 |
| `63-lifespan.md` | Lifespan 管理（启动/关闭钩子） |
| `64-pagination.md` | 分页（cursor-based） |
| `65-tool-fingerprinting.md` | Tool 指纹（变更检测） |
| `66-versioning.md` | Server 版本管理 |

## 📱 MCP Apps（Server-rendered UI）

| 文件 | 内容 |
|---|---|
| `70-apps-overview.md` | MCP Apps 概览 (SEP-1865) |
| `71-apps-quickstart.md` | 第一个 App |
| `72-apps-architecture.md` | App 架构、iframe 沙箱 |
| `73-apps-development.md` | App 开发指南 |

---

## 开发速查路径

**新建 MCP server**: `01` → `10` → `11` → `15` → `40` → `41` → `30`

**从 v3 升级**: `03` → `02` → `15` → `17` → `50`

**加认证**: `51` → `50` → `52` → `53`

**加可观测性**: `40` → `42`

**加后台任务**: `16` → `22`

**加 UI**: `70` → `71` → `72` → `73`
