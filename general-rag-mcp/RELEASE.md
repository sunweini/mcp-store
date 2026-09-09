# general-rag-mcp — 发布指南

## 版本管理

遵循 [SemVer](https://semver.org/)：
- `MAJOR`: breaking changes（tool 签名变更、删除 tool）
- `MINOR`: 新增功能（新 tool、新 resource）
- `PATCH`: bug fix、文档更新

## 发布流程

### 1. 本地验证

```bash
uv run python -m pytest tests/ -q

# 启动 server，冒烟验证
uv run python server.py
uv run python client.py

# 检查 protocol 兼容性
curl -s -X POST http://127.0.0.1:9055/mcp \
  -H "MCP-Protocol-Version: 2026-07-28" \
  -H "Mcp-Method: tools/list" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientInfo":{"name":"check","version":"1.0"},"io.modelcontextprotocol/clientCapabilities":{}}}}' | python3 -m json.tool
```

### 2. 构建镜像 & 部署

```bash
# 继承仓库基础镜像 mcp-base（见 deploy/Dockerfile.base）
cd ../deploy
docker compose build general-rag-mcp
docker compose up -d general-rag-mcp
```

### 3. 注册到 Gateway

1. 访问 `http://localhost:8081`（gateway-admin）
2. Servers 页添加：
   - Name: `general-rag-mcp`
   - URL: `http://general-rag-mcp:9055/mcp`（容器内网互访）
   - Description: `检索 general-rag 内部知识库（章管家接口文档等）`
3. 创建 API Token 并勾选本 MCP 的 read/write 权限
4. 若注册后 tools 为 0，用「refresh-tools」修复（竞态已知问题）

## 环境要求

- Python >=3.12
- 后端 general-rag 网络可达（`GENERAL_RAG_BASE_URL`）

## 可观测性

- 结构化日志 → stdout（key=value，`LOG_FORMAT=json` 切 JSON）
- OpenTelemetry traces → 配置 `OTEL_EXPORTER_OTLP_ENDPOINT`
- Prometheus metrics → `/metrics`（`PROMETHEUS_PORT`，默认 9464）

## Changelog

### 0.4.0（2026-09-09）

- `knowledge_base_search` 返回**可渲染图片**：`source.images`（§4.3 图片 URL）拉字节转成 MCP `image` content 块随回答返回，客户端原生渲染；不再只是裸 URL 字符串。
- **边界**：单张 >2MB、拉取失败（404/网络）→ 该图降级回 URL（留在 `sources[].images`，作文本降级，不报错）；最多返回前 3 张，多的只保留 URL。
- **实现**：`RagClient.get_media(url)` GET `/api/v1/media/<relpath>`（取证路由，白名单图片 / §5.6；用 `_origin` 去 `/api/v1` 后缀拼全路径）；`mimeType` 从图扩展名推断。`knowledge_base_search` 改用 `ToolResult(content=[TextContent(answer), ImageContent(...)], structured_content=<原dict>)`——结构化字段不变，仅 content 新增 image 块。
- `rag_client` 新增 `get_media`（非 200 抛 RagError）；`_origin` 在 `__init__` 由 base_url 去掉 `/api/v1` 派生。
- **测试**：39→46（images 透传、image 块生成、≤3 张上限、>2MB 降级、拉取失败降级、mime 推断、无图仅文本块）。

### 0.3.0（2026-09-09）

- 新增 3 工具：`knowledge_base_golden_suggest`（只读，反推问法初稿）、`knowledge_base_golden_add`（写，两步确认写入 /golden/cases）、`knowledge_base_delete_document`（写，三步流删除 + golden_impact 警示）。对应 general-rag issue #6 契约 §8 tool 4/6。
- **golden_suggest**：自持 502/空 candidates 重试（间隔 ≥5s ×3）；404 = 文档不在库不重试（红线：不能硬凑问法）；耗尽如实报错不编造。输出仅供展示，不得直接写入。
- **golden_add**：`source="mcp"` 恒带；空 query/doc_id 挡、negatives 非 list 强转、去空串负样本；幽灵引用（期望命中/负样本不在库）交后端 400 透传。
- **delete_document**：`dry_run` 默认 true；纯透传不盲包 `status:"ok"`（preview 信号保留）；`golden_impact.cases>0` 提醒悬空 case 待清理，工具不自动删。
- **测试**：18→36（suggest 502退避/空candidates重试/404不重试/耗尽 + add 校验透传 + delete preview保留/默认dry_run + rag_client 写操作不重试）。

### 0.2.0（2026-09-05）

- 新增工具 `knowledge_base_ingest_file`：按服务器端绝对路径摄入大文件（write tool）。
- **背景**：file_bytes 参数要求 LLM 在 tool-call 里生成完整内容，20-44KB 文本会被模型输出截断（实测 3 份 22-44KB 字段清单只入库 286-683 字符）。新工具只传一个短路径字符串（`file_path`），内容由后端从服务器磁盘读取，永不截断。
- 后端配套：`POST /api/v1/ingest-path`（同管线读服务器路径，双路径探测 `/opt/general-rag/ingest` ↔ `/app/ingest`）。
- **用法**：调用方先 rsync/scp 文件到 MCP 同机的 `/opt/general-rag/ingest/`，再调 `knowledge_base_ingest_file(file_path, namespace)`。大文件不再使用 `knowledge_base_ingest`（file_bytes）。
- 测试 13→18（ingest_file 参数透传/空路径/错误透传/不重试 + rag_client.ingest_path）。

### 0.1.0（2026-09-01）

- 初始版本：search / namespaces / health / ingest 四工具
