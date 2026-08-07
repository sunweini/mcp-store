# 知识库

仓库内积累的开发知识，分两类：

## 自研经验（本项目实践沉淀）

| 文件 | 内容 |
|---|---|
| `search-mcp-key-pool-pattern.md` | 多 API key 池设计模式：Redis schema、配额感知轮换、错误分类状态机、热更新+断线自愈、三源（tavily/brave/serpapi）API 差异速查 |
| `mcp-production-deployment-pitfalls.md` | 受限网络生产部署踩坑：uv.lock 阿里云镜像重建、容器内外网差异、Redis 数据目录 uid 权限（MISCONF）、pubsub 断线、代理配置、git archive 部署流程 |
| `mcp-account-level-permission-pattern.md` | MCP 账户级细粒度权限模式：比 server 更细的 token→账户授权（gateway 零改动靠 Authorization 透传、MCP 为权威、token_accounts 映射 + write⇒read 不变式、授权矩阵 union 同步 gateway 粗闸） |

## 官方文档（FastMCP v4）

`fastmcp-v4/` — FastMCP 4.0.0b1 + MCP Protocol 2026-07-28 官方文档 39 篇，索引见 `fastmcp-v4/README.md`。

**强制规则**：写代码前必须先读对应知识库文件（触发场景见根 CLAUDE.md「知识库」节）。
