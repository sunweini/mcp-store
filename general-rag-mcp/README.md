# general-rag-mcp

general-rag 知识库检索 MCP server：让任意 agent 通过 MCP 检索
general-rag 知识库（章管家接口文档等），返回可置信、带来源标记的答案。
后端无鉴权（内网信任环境），检索按 namespace 分区。

## 功能

| Tool | 类型 | 说明 |
|---|---|---|
| `knowledge_base_search` | 读 | 检索问答（核心）；namespace 缺省 stamp-project，支持 doc_type/tags/categories/use_graph 过滤 |
| `knowledge_base_namespaces` | 读 | 列出可用命名空间，供 search 校验/提示 |
| `knowledge_base_health` | 读 | 探测组件状态（es/neo4j/llm/embedding/rerank） |
| `knowledge_base_ingest` | **写** | 上传文件摄入（docx/pdf/pptx/txt/md，≤20MB） |

## 快速开始

### 连接配置

通过 Gateway（生产，端口 8082）：

```json
{
  "mcpServers": {
    "gateway": {
      "url": "http://localhost:8082/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

直连（开发调试）：

```json
{
  "mcpServers": {
    "general-rag-mcp": { "url": "http://localhost:9055/mcp" }
  }
}
```

### 从源码运行

```bash
cd general-rag-mcp
uv sync --all-extras
uv run python server.py          # 默认连 10.33.17.72:8001/api/v1
uv run python client.py          # 冒烟：namespaces / health / search
uv run python -m pytest tests/ -q
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `GENERAL_RAG_BASE_URL` | `http://10.33.17.72:8001/api/v1` | general-rag 后端基址 |
| `GENERAL_RAG_TIMEOUT` | `60` | 外部调用超时（秒，LLM 合成较慢） |
| `MCP_HOST` | `127.0.0.1` | 监听地址（容器内 0.0.0.0） |
| `MCP_PORT` | `9055` | MCP 端口（仓库登记） |
| `LOG_FORMAT` | `console` | `console` / `json` |
| `PROMETHEUS_PORT` | `9464` | Prometheus /metrics 端口 |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | 无 | OTLP collector URL |

## 行为语义（agent 须知）

- **namespace 分区**：检索必须先确认 namespace；`knowledge_base_search`
  缺省落到 `stamp-project`（而非可能被禁的 `default`）。
- **degraded**：为 true 时检索可能降级，向用户说明"部分能力降级"。
- **gap_warning**：非空表示知识库可能覆盖不足，不要臆测补充。
- **来源置信度**：`confidence_basis=rerank` 时星级有绝对意义；
  `source_path=graph_expand` 的可信度略低，优先以 direct 为主。

## 安全

- 只允许内网访问，不暴露公网；检索结果不回传不可信外网服务。
- 日志与返回体对 snippet 截断（500 字），不完整转储文档内容。

## 协议

基于 MCP `2026-07-28` specification，stateless HTTP transport。
健康探活使用 MCP 标准 `ping`（FastMCP 原生支持）。
