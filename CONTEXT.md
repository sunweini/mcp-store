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

## general-rag: golden 集（检索回归尺子）

**Golden case**:
检索质量回归尺子的一条用例 = 口语问法 + 期望命中 doc_id + 库内负样本断言。存后端服务器，经 `/golden/*` REST API 读写。
_Avoid_: 回归用例、golden 用例

**期望命中**:
一条 golden case 的"标准答案"——该口语问法应命中的那条 doc_id。
_Avoid_: 正确答案、ground truth

**负样本**:
case 里断言"不应命中"的 doc_id（须在库内），用于剔除过度召回。
_Avoid_: 反例、负面样本

**幽灵引用**:
case 的期望命中或负样本 doc_id 已不在知识库（文档被删/迁移后悬空），后端拒收。
_Avoid_: 悬空引用、孤儿引用（与删除后的 orphaned case 区分）

**Golden_impact**:
一次文档删除牵动的 golden case 数 + case_ids。只警告、绝不自动删 case。
_Avoid_: 影响面、golden 影响

**反推初稿**:
`golden_suggest` 从一篇已摄入文档反推出的口语问法候选（golden 集生长的种子），仅供展示，不得直接写入。
_Avoid_: 建议、草案

**两步确认**:
摄入生长惯例——反推初稿 → 维护者点头 → 才写入 golden 集。
_Avoid_: 审核、审批流

**三步流**:
删除工具流程——dry_run 预览（含 golden_impact）→ 用户确认 → 真删；删后悬空 case 由人工另行清理。
_Avoid_: 删除流程、确认流
