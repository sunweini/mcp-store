# MCP Store

多 MCP 网关平台。一个 gateway 聚合若干后端 MCP server，用 token 做认证与读写权限控制，全量调用审计。

## Language

**MCP server**:
一个业务 MCP（如 zabbix-mcp、tavily-mcp、general-rag-mcp）。以独立目录/容器存在，暴露若干工具。
_Avoid_: 后端、被代理服务

**Gateway**:
聚合所有 MCP server 的代理，负责 token 认证、按 (server, mode) 做读写授权、全量审计调用。
_Avoid_: 代理服务器、中间层

**Namespace**:
MCP 工具名的命名空间前缀行。gateway 用 `{server}_{tool}` 切分出目标 server（server 名禁下划线，所以第一个 `_` 是分隔符）。数据分区也会用 namespace，两者是这个仓库的两个含义，需按上下文区分。
_Avoid_: 前缀、命名空间（易与数据分区混淆）

**Mode**:
一个工具的读写分类，"read" 或 "write"（write = 后端工具的 `annotations.destructiveHint` 为真）。它是授权判定的依据。不同工具、不同 server 各自有 mode。
_Avoid_: 权限级别、操作类型

**Token permission**:
一个 token 的授权矩阵，`{server: {read, write}}`。是 proxy 授权判定的依据（permissions）。账户级权限（aliyun-dns-mcp）在此之外另有权威来源。
_Avoid_: token 权限矩阵、授权设置

**Authorize**:
"给定命名的工具 + token 的 permissions + 当前已知的 mode，判断是否放行"的完整判定链路（server/tool 解析 → mode 查询 → grant 判断）。
_Avoid_: 鉴权、授权检查

**Audit record**:
一次工具调用的审计记录（成功+失败全量），写入 `audit:calls` stream，由 admin 消费者落库到 MySQL calls 表。
_Avoid_: 调用日志、审计条目
