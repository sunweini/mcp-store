# 知识库

## 自研模式（patterns/）
| 文件 | 模式 | 适用场景（触发条件） |
|---|---|---|
| `patterns/search-mcp-key-pool-pattern.md` | 多 API key 池 | 新搜索类 MCP / 需要 key 轮换 |
| `patterns/mcp-account-level-permission-pattern.md` | 账户级权限 | token 需要比 server 更细粒度权限 |
| `patterns/audit-async-stream-pattern.md` | 审计异步化 | 网关/高并发写审计 |

## 踩坑记录（pitfalls/）
| 文件 | 教训 | 适用场景 |
|---|---|---|
| `pitfalls/mcp-production-deployment-pitfalls.md` | 受限网络部署 | 生产构建/部署 |

## 官方文档（FastMCP v4）
`fastmcp-v4/` — 索引见 `fastmcp-v4/README.md`。

**强制规则**：写代码前必须先读对应知识库文件（触发场景见根 CLAUDE.md「知识库」节）。
