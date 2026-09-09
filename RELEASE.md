# 版本更新说明

本仓库版本更新记录。每个里程碑记录：变更内容、影响范围、升级/部署注意事项、回滚方式。

## v1.4.1 — general-rag-mcp search 返回可渲染图片（2026-09-09）

**目标**：`knowledge_base_search` 把 `source.images`（§4.3 图片 URL）拉字节转成 MCP `image` content 块随回答返回，客户端原生渲染；不再只给裸 URL 字符串。

### 变更内容

| 变更 | 说明 |
|---|---|
| `knowledge_base_search` 返图 | `content=[TextContent(答案), ImageContent(...)]` + `structured_content=<原 dict>`，结构化字段不变 |
| 边界 | 单张 >2MB / 拉取失败 → 降级回 URL（留在 sources[].images，不报错）；最多前 3 张 |
| RagClient.get_media | GET `/api/v1/media/<relpath>`（取证路由 §5.6，白名单图片，越权 404）；用 `_origin`（base_url 去 /api/v1）拼全路径避免双写 /api/v1 |
| mime 推断 | 按 URL 扩展名 → image/png | jpeg | gif | webp 等，未知回退 png |

### 影响范围

- 仅 general-rag-mcp 的 `knowledge_base_search` 返回形态增强；参数、工具名、gateway 转发不变。
- 后端 general-rag 已上线 `/media` 取证路由，无需后端改动。

### 部署注意事项

1. 重建 general-rag-mcp 容器：`docker compose build general-rag-mcp && docker compose up -d general-rag-mcp`。
2. 需与后端 `/api/v1/media` 连通（白名单图片）；越权/不存在按 404 → 该图降级回 URL。

### 回滚

```bash
docker compose rm -sf general-rag-mcp
# 或 git 回退 general-rag-mcp/ 目录后重建
```

### 验证证据

- general-rag-mcp 46 用例全绿。
- live 冒烟（imgtest-mcp）：MCP `tools/call` 返回 `image` content 块（mimeType=image/png，base64 PNG），`GET /api/v1/media/...` → 200 image/png。

---

## v1.4.0 — general-rag-mcp golden 生长+删除 三工具（2026-09-09）

**目标**：补 general-rag-mcp 的 golden 集生长（两步确认）与文档删除（三步流 + golden_impact）三工具，对应 general-rag issue #6 契约 §8 tool 4/6。

### 变更内容

| 变更 | 说明 |
|---|---|
| general-rag-mcp 新增 3 工具 | `knowledge_base_golden_suggest`（读，反推问法初稿）/ `knowledge_base_golden_add`（写，两步确认写入 /golden/cases，source="mcp"）/ `knowledge_base_delete_document`（写，三步流删除，dry_run 默认 true，golden_impact 警示）。工具数 4→8 |
| suggest 语义 | 自持 502/空 candidates 重试（≥5s×3）；404 红线不重试（文档不在库不能硬凑）；耗尽如实报错不编造；初稿仅供展示 |
| add 语义 | 空 query/doc_id 挡、negatives 非 list 强转/去空串；幽灵引用（期望命中/负样本不在库）交后端 400 |
| delete 语义 | 纯透传保留 `status:"preview"`；dry_run 默认 true；`golden_impact.cases>0` 提醒悬空 case 人工清理，工具不自动删 |
| 可观测性 | `RagClient._request` 新增 `transient_statuses`；瞬态 502 记 WARNING 不误报 ERROR（golden_suggest 传 {502}） |
| 测试 | general-rag-mcp 39 用例全绿；live 冒烟 dev ns 全流程零残留 |

### 影响范围

- 纯 general-rag-mcp 功能新增；gateway-proxy/admin/其他 MCP 不动。后端 general-rag 已上线（/golden/*、/delete）。
- 现有 5 工具行为不变（search/namespaces/health/ingest/ingest_file）。

### 部署注意事项（必读）

1. 重建 general-rag-mcp 容器：`docker compose build general-rag-mcp && docker compose up -d general-rag-mcp`。
2. 注册 gateway 后若 tools:0，用「refresh-tools」修复（已知竞态）。
3. 写操作（golden_add/delete_document）token 需显式授 write 权限；读取 golden_suggest 授 read。

### 回滚

```bash
docker compose rm -sf general-rag-mcp
# 或 git 回退 general-rag-mcp/ 目录后重建
```

### 验证证据

- general-rag-mcp 39 用例全绿；MCP tools/list 8 工具注解正确（suggest read / add、delete destructive）。
- live 冒烟（dev ns）：suggest 返 2 candidates；add 写入 case dev-0002(source=mcp)；幽灵引用 400 拒收；delete preview `status:"preview"` + golden_impact.cases=1；真删 chunks_deleted=1；清理后 dev `golden/health` total_cases=0。

---

## v1.3.0 — 权限编辑功能 + 读写越权修复（2026-09-01）

**目标**：补上 token 的 MCP server 权限编辑能力（创建后能改），并修复一个导致只读 token 越权访问 write 工具的 gateway-proxy 缺陷。

### 变更内容

| 变更 | 说明 |
|---|---|
| 新增 `PUT /api/tokens/{id}` | gateway-admin：更新 token 的 server 级 read/write 权限；校验 server 存在；只更新请求里出现的 server；aliyun-dns-mcp 由账户矩阵 union 权威、直接跳过；更新后 publish `token:changed` 即时失效 |
| 前端「编辑MCP权限」按钮 | token 页新增（与「授权」并存）；弹窗列出 non-aliyun server 的 RW 勾选；全量发送（含全 false 的），修复"取消权限不生效" |
| **gateway-proxy 越权修复** | `registry._mount_one` 原在 introspect 失败（返回 `[]`）时用空结果覆盖 `TOOL_REGISTRY`，导致 `get_tool_mode` 退回默认 `read`，只读 token 可越权看到/调用 write 工具。修复：tools 非空才注册并写 redis，失败保留上一次成功的 mode 元数据 |
| 回归测试 | gateway-admin `test_update_token_*`（含全 false 取消）+ gateway-proxy `test_registry.py`（introspect 失败不污染 TOOL_REGISTRY / 只读仅见 read 工具） |

### 影响范围

- gateway-admin：token 页新增「编辑MCP权限」，不影响「授权」（阿里云账户矩阵）。
- gateway-proxy：权限判定更严格（fail-closed），修复越权；需重建镜像。
- general-rag-mcp：本轮无代码变更（v1.2.0 已交付）。

### 部署注意事项（必读）

1. **gateway-proxy 需重建**（`docker compose build gateway-proxy && up -d`）——越权修复在 proxy 侧。
2. **gateway-admin 需重建**（前端 dist 内嵌于镜像，`npm run build` 生成）。
3. **workbuddy token 权限已实测**：zabbix `{read:true, write:false}`，其余 read+write；该配置现在能正确执行（只读 token 不再见/调 write 工具）。
4. **写工具测试注意**：只读 token 调 write 工具会返回 `Permission denied`；只有 write 权限才能到后端。

### 回滚

```bash
git checkout <v1.2.0 前 commit> && 重建 gateway-proxy / gateway-admin 镜像
# 注：token 权限数据结构不变，回滚无数据迁移
```

### 验证证据

- gateway-admin 150 用例 + gateway-proxy 79 用例全绿
- 4 种权限组合（read-only / write-only / read+write / none）× tools/list + tools/call 全矩阵通过
- 只读 token 调 write 工具 → `Permission denied: permission_denied`（不再透传 Zabbix 后端）

---

## v1.2.0 — 新增 general-rag-mcp（2026-09-01）

**目标**：新增独立 MCP server，让任意 agent 通过 MCP 检索 general-rag 知识库（章管家接口文档等）。

### 变更内容

| 变更 | 说明 |
|---|---|
| 新增 `general-rag-mcp/` | 4 个工具：`knowledge_base_search`（读，namespace 缺省 stamp-project）/ `knowledge_base_namespaces`（读）/ `knowledge_base_health`（读）/ `knowledge_base_ingest`（写，⚠️ 写操作） |
| 后端对接 | general-rag REST API `http://10.16.12.12:8001/api/v1`（无鉴权）；503 指数退避重试，ingest 不重试；snippet 截断 500 字（隐私） |
| 端口登记 | 容器内 9055（不映射宿主）；metrics 宿主 9470 → 容器 9464 |
| 可观测性 | structlog + OTel traces + Prometheus metrics（`general_rag_mcp_*` 指标族） |
| 测试 | 14 用例全绿（httpx MockTransport，不连真实后端） |

### 影响范围

- 纯新增：不影响现有 5 个 MCP 与 gateway 调用方式。
- 新增 compose service `general-rag-mcp`，无 Redis 依赖（后端无鉴权无 key 池）。

### 部署注意事项（必读）

1. **后端网络可达**：MCP 容器需能访问 `10.16.12.12:8001`（内网）。若地址变化改 `GENERAL_RAG_BASE_URL`。
2. **注册 gateway**：admin → Servers 添加 `general-rag-mcp`，URL `http://general-rag-mcp:9055/mcp`；创建 Token 勾选 read/write。
3. **tools: 0 竞态**：若注册后工具为空，用「refresh-tools」修复（已知问题）。
4. **写权限**：`knowledge_base_ingest` 是写操作，token 需显式授予 write 权限。

### 回滚

```bash
# 移除 service 与注册（不影响其他服务）
docker compose rm -sf general-rag-mcp
# git 回退根 CLAUDE.md / docker-compose.yml / RELEASE.md 及 general-rag-mcp/ 目录
```

---

## v1.1.0 — 并发加固（2026-08-07）

**目标**：10-50 QPS → 千级 QPS。**不影响现有功能与调用方式**（公开工具签名、调用方式、端口全部不变）。

### 变更内容

#### gateway-proxy（审计异步化 + 并发加固）
| 变更 | 说明 |
|---|---|
| 审计改单流 XADD | proxy 不再直连 MySQL；全量调用（成功+失败）XADD 至 `audit:calls` stream（MAXLEN 50000） |
| 删 MySQL 直写 | `db.py` 删除、`MYSQL_URL` 从 proxy 移除（`pyproject.toml` 删 aiomysql 依赖 + uv.lock 重建阿里云镜像） |
| Token 本地缓存 | TTL 60s + LRU 1000；`token:changed` 通道即时失效（admin create/delete/权限变更三处 publish）；Redis 故障缓存降级放行（防 403 风暴） |
| Client 复用 | 修复每请求新建 Client + httpx2 连接池（TCP+TLS 握手每请求一次 → 共享连接池） |
| 背压 + 总超时 | per-backend semaphore（默认 100）+ 调用总超时 90s（per-server `call_timeout` 可配，默认 90s ≥ 后端最长任务 60s） |
| pubsub 自愈 | watch_changes 断线重建订阅（`server:changed` + `token:changed` 双频道同连接） |
| Redis socket_timeout | 5s（防 Redis 挂起阻塞请求路径） |
| 新指标 | `audit_dropped_total` / `token_cache_hit_total` / `token_cache_miss_total` |

#### gateway-admin（审计消费者）
| 变更 | 说明 |
|---|---|
| 审计消费者 | lifespan 后台 task：XREADGROUP `audit:calls` batch=100/block=1s → executemany INSERT calls 表 → XACK |
| 死信流 | 落库失败即移 `audit:calls:dead`（每次失败即死信，无重试累积）+ XACK 防 PEL 无限重投 |
| 自愈循环 | Redis 闪断消费者不退出（1s 退避重试） |
| 新指标 | `audit_batch_size` / `audit_batch_latency` / `audit_queue_depth`（无 opentelemetry 依赖时降级 warning，不阻断） |
| 探活超时 | `call_timeout` 字段支持（ServerCreate/Update，None → proxy 默认 90s） |

#### 搜索 MCP（tavily / brave / serpapi）
| 变更 | 说明 |
|---|---|
| httpx client 复用 | 共享 client 单例（连接池 100/50）；key 走请求级凭证（header 或 query）；per-request timeout；**公开方法签名与 factory 签名不变** |
| KeyPool 借用语义 | in-flight 计数分散并发请求到不同 key（防 429 风暴）；实例级锁（不包外呼 await）；reload 锁内整表替换（防记账竞态） |
| Redis 往返合并 | on_success 的 hset+zadd+expire 三连 → pipeline 一次往返 |
| 429 退避 | 重试前指数退避（0.5s 起步） |
| 并发上限 | per-endpoint semaphore（search/extract/map=20，crawl/research=5） |

#### zabbix / aliyun-dns
- **无代码变更**（已核实为正确模式：进程级单例 client / 账户缓存 SDK client）
- 并发规范沉淀：`templates/mcp-template/CLAUDE.md` 新增「并发与性能规范」C1-C6（必读）

### 架构变化

```
改造前：MCP Client → proxy → (MySQL 同步写 + Redis 双写) + 每请求新建 Client
改造后：MCP Client → proxy → XADD audit:calls ──> gateway-admin 消费者 ──> MySQL
                            └── 共享连接池 + token 缓存 + 背压/超时
```

### 部署注意事项（必读）

1. **部署顺序（防审计断档）**：先 `docker compose up -d gateway-admin`（消费者先起建组）再 `gateway-proxy`（切 stream 写）。一键 `deploy.sh` 秒级窗口可接受（消费者 `>` 从 stream 头补拉）。
2. **proxy.env 移除 MYSQL_URL**：proxy 不再需要（代码零读取，保留无害但应清理）。
3. **部署后必查**：`bash deploy/verify_audit_pipeline.sh` 对账 stream XLEN 与 calls 表 COUNT。
4. **tools: 0 竞态**：新起 MCP 容器时 proxy 挂载可能早于容器就绪 → 注册工具为空。管理界面「refresh-tools」修复（本版本部署实测 brave/serpapi 均需此步）。
5. **旧 `audit:failures` 流**：本版本已删除（旧代码残留数据按需清理）。
6. **审计延迟**：失败面板从"实时"变"准实时"（<1s 落库，XREADGROUP block 1s + batch 100）。
7. **p95 变化**：latency 含 semaphore 排队时间（真实端到端，预期行为）。

### 回滚

```bash
# git 回退到 v1.0.0 前（dc5826f 之前）
git checkout <old-commit> && git archive ... # 同部署流程
# 注意：回滚后 proxy 恢复直写 MySQL，审计不中断（calls 表 schema 未变）
```

### 验证证据

- 本地压测：mock + 真实链路（Redis + tavily + gateway）三档 100/500/1000 并发全 PASS，零失败、审计流全对齐、denied 正确拒绝
- 生产端到端：调用 → stream → 消费者 → calls 表全链路验证通过
- 测试基线：proxy 79 / admin 141 / tavily 77 / brave 70 / serpapi 77 全绿

---

## v1.0.0 — 初始版本（2026-07-31 ~ 2026-08-06）

- 网关平台：gateway-proxy + gateway-admin（Server/Token/API Keys 管理 + 监控面板）
- 搜索 MCP 三源：tavily / brave / serpapi（多 key 池 + 配额告警）
- zabbix-mcp 告警巡检（2026-08-03 迁入 9053）
- aliyun-dns-mcp 账户级权限（2026-08-06）
- 容器化部署 10.33.17.72（2026-07-31 Docker Compose 迁移）
