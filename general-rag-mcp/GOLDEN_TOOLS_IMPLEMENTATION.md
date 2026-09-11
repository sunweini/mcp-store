# 实现任务书：golden 生长工具 + 删除工具（agent 可读，自包含）

> ## ✅ 已完成（2026-09-11 核实）
>
> **本任务书的三件套已全部实现并上线**：`knowledge_base_golden_suggest` /
> `knowledge_base_golden_add` / `knowledge_base_delete_document`。
> 本项目现有 **8 个工具**（search / namespaces / health / ingest / ingest_file /
> golden_suggest / golden_add / delete_document），已推 `mcp-store` 远端。
>
> **这份文档保留作历史记录，不要再照着它动手。** 下文"现有 5 个工具""从未实现"
> 等表述描述的是**任务开始前**的状态，已不再成立。
>
> ⚠️ 之所以加这段横幅：一份已完成的任务书若保持"待做"的措辞，会误导接手的 agent
> **重做已完成的工作**。判断某能力是否落地，看工具清单或跑 `pytest`，
> **不要看文档里的状态措辞**。

> 给接手 agent 的完整说明。你不需要任何对话上下文——后端 API 契约已内联，代码模式请看本项目的
> `tools/knowledge_base.py`（现有 5 个工具的注册写法）与 `rag_client.py`（REST 封装）。动手前先读这两个文件。

## 0. 背景（为什么做）

后端项目 general-rag（另一仓库，本机 `/Users/sunweini/同步空间/工作内容/AI工作项目/general-rag`）已完成检索回归尺子基建：

- **golden 集**（检索质量回归用例）：每条 case = 口语问法 + 期望命中 doc_id + 库内负样本断言。存后端服务器、经 `/golden/*` REST API 读写。
- **摄入生长惯例**（后端 ingestion-spec §8）：每摄入一篇新文档，应给它补 1-2 条口语问法进 golden 集，**两步确认**（agent/suggest 反推初稿 → 维护者点头 → 才写入）。
- 契约全文：后端仓库 `docs/mcp-integration-guide.md` §5.4（/delete）/§5.5（/golden/*）/§8（tool 4 delete、tool 6 golden 两件套）。**契约的 golden/delete 工具侧从未实现**——本项目现只有 5 个工具（search/namespaces/health/ingest/ingest_file），本任务补齐 3 个：`knowledge_base_golden_suggest`、`knowledge_base_golden_add`、`knowledge_base_delete_document`。

红线（后端用户的验收边界）：**不能为了命中而命中**——初稿问法必须是库外真实语义、答案必须真在这篇文档里；问法必须口语化（用户视角），禁止"标题复读"式假问法；后端没有的内容不得硬凑。库外超纲问题不入集。

## 1. 后端 API 契约（已上线，直接用；base 已含 /api/v1）

生产：`http://10.33.17.72:8001/api/v1`（如无本地后端就跑这个；本机项目 CLAUDE.md/README 有本地起法）。
无鉴权（可信内网）。超时按 rag_client 惯例 ≥60s。错误：4xx = 业务错误（detail 中文可读），503 = ES 不可达，502 = suggest 的 LLM 调用失败。

### 1.1 POST /golden/suggest —— 反推问法初稿（无副作用）

请求 `{"namespace": str, "doc_id": str}`（doc_id = 相对路径，如 `kingdee-galaxy/PUR_Requisition_采购申请单.md`）
成功 200：`{"status":"ok","namespace":...,"doc_id":...,"candidates":[{"query":"口语问法","negatives":["<亲戚 doc_id>",...]}]}`（≤2 条；negatives 已过后端在库校验）
错误：404 = 文档不在库（**不能反推**——红线）；400 = ns 非法/路径穿越/LLM 未配置；502 = LLM 运行时失败（**瞬态常见**：deepseek 批量调用偶发空内容，调用方应带间隔重试 ≤3 次，间隔 ≥5s，仍失败才如实报错）。

### 1.2 GET /golden/cases —— 读 case 列表（可选支持）

`GET /golden/cases?namespace=<ns>`（省略 = 全部）→ `{"namespace":...,"total":N,"cases":[{id,doc_id,query,negatives:[],created_at,source,namespace}]}`

### 1.3 POST /golden/cases —— 写入一条（两步走的第二步，写操作）

请求 `{"namespace":str,"doc_id":str,"query":str,"negatives":[str,...],"source":str}`（source 建议 `"mcp"`；query 非空；negatives 可空）
成功 200：`{"status":"ok","case":{...含 id（形如 <ns>-0001）...}}`
错误：400 = ns 非法 / 路径穿越 / **期望命中或负样本 doc_id 不在库（幽灵引用拒收）** / 空 query；503。

### 1.4 PUT/DELETE /golden/cases/{id} —— 修订/删单条（写操作）

修订 `PUT /golden/cases/{id}` body `{"namespace":str, 其余字段可省略}` → 200 `{"status":"ok","case":{...}}`；404 case 不存在；400 同上校验。
删除 `DELETE /golden/cases/{id}?namespace=<ns>` → 200 `{"status":"ok","deleted":"<id>"}`；404。
注意：case id **单调不复用**；修订同过两步确认（改尺子 = 建尺子）。

### 1.5 GET /golden/health —— 尺子健康（可选，如做增强）

`GET /golden/health?namespace=<ns>` → `{status, existence_violations[], uncovered_docs[], stale_cases[], corrupt_files[], summary{total_cases,violations,uncovered,stale,corrupt}}`

### 1.6 POST /delete —— 删文档（真删，三步流 + golden_impact）

请求 `{"namespace":str,"doc_id":str|filename:str,"dry_run":bool}`（doc_id 与 filename 二选一，doc_id 优先）
`dry_run=true` → `{"status":"preview","namespace":...,"doc_id":...,"file_path":...,"chunks":N,"graph_entities":N,"golden_impact":{"cases":N,"case_ids":[...],"warning":可缺省}}`
`dry_run=false` → `{"status":"ok",...,"chunks_deleted":N,"graph":{status:ok|skipped|error},"golden_impact":{...}}`
错误：400（filename 多篇命中/路径非法）、404（不存在或归属不符）、503。
**golden_impact 语义**：该 doc 牵动几条 golden case。**只警告、绝不自动删 case**——清理必须经用户确认后走 `DELETE /golden/cases/{id}`。

## 2. 要实现的三个工具（全部加在 tools/knowledge_base.py）

沿用现有模式：async 函数 `(…, client: RagClient | None = None)` → 内部 `client = client or get_client()`；
RagClient 走 `_request(method, path, json=...)`（看现有 search/health/ingest 怎么调的，照抄风格）；
`register(mcp, get_client, metrics)` 里加 `mcp.tool(...)` 注册块，annotations：只读用 `ToolAnnotations(readOnlyHint=True)`，写操作用 `ToolAnnotations(destructiveHint=True)`。

### 2.1 `knowledge_base_golden_suggest`（只读）

- 参数：`namespace: str`、`doc_id: str`
- 调用：`POST /golden/suggest`
- 返回：后端 JSON 直接返回（含 candidates）
- description 要点：**只用于摄入新文档后反推 golden 问法初稿**；返回初稿**仅供展示**，不得直接写入；LLM 偶发空内容 → 间隔 ≥5s 重试 ≤3 次，仍失败如实报错不编造；对"标题复读"式明显假问法（与文档标题几乎一样）应提醒用户或建议弃用。

### 2.2 `knowledge_base_golden_add`（写，destructiveHint）

- 参数：`namespace: str`、`doc_id: str`、`query: str`、`negatives: list[str] | None = None`
- 调用：`POST /golden/cases`（source 固定 `"mcp"`）
- description 要点（**强制三步流语义，写进 description 让调用 agent 遵守**）：
  1. 先经 `knowledge_base_golden_suggest`（或调用方自拟初稿）；
  2. **向用户展示初稿（问法 + 期望命中 + 负样本候选），获明确确认**——用户可改可删；
  3. 确认后才调用本工具写入。
  红线：禁止未经确认写入；禁止"标题复读"式假问法；问法的答案必须在该文档里（超纲不写）。
- 返回：`{"status":"ok","case":{...}}`（含新 case id）

### 2.3 `knowledge_base_delete_document`（写，destructiveHint —— 契约 §8 tool 4，之前从未实现）

- 参数：`namespace: str`、`doc_id: str | None = None`、`filename: str | None = None`、`dry_run: bool = True`（默认预览！）
- 调用：`POST /delete`
- description 要点（**强制三步流，同现有 ingest 的确认规范**）：
  1. 先以 `dry_run=true` 预览，把将删清单**连同 `golden_impact`（牵动 N 条 golden case）**展示给用户；
  2. 用户明确确认后才 `dry_run=false` 真删；
  3. 若 golden_impact.cases > 0：删除完成后**提醒用户**牵动的 case 已悬空，建议后续经人工确认后用 golden case 删除端点清理（本工具不得自动删 case）。
  禁止跳过预览直接真删；真删不可逆。

### 2.4（可选加分项，非必须）`knowledge_base_golden_health` 或在 health 工具返回里补 golden 摘要

`GET /golden/health` → 三查 + corrupt 摘要。若做，保持只读 hint，description 说明用途（摄入/删除后自查尺子）。

## 3. 测试与验收清单

项目有 `tests/`（看现有测试怎么 mock RagClient 的，照风格补）。除此之外做一遍 live 冒烟（后端生产在跑，golden 集已有 64 条真实 case——**测试写入后必须清理，别污染**）：

1. `knowledge_base_golden_suggest`：对真实 doc（如 `it-alert-sop/sop-home-disk-10.33.16.42.md`）→ 应返回 1-2 条口语 candidates；
2. `knowledge_base_golden_add`：对 **dev namespace 的一个临时 doc**（可先 `/ingest` 一个小 md 到 dev，再 suggest/add，验完**删掉该 case + 删掉该 doc**）——验证幽灵引用拒收（乱写 doc_id 应 400）；
3. `knowledge_base_delete_document`：`dry_run=true` 预览一个 dev 临时 doc → 响应含 golden_impact 字段 → 不真删也验证"默认 dry_run"行为；
4. 全量 `pytest` 通过；
5. 完成后按 RELEASE.md 流程登记版本（新 tool = MINOR bump），并把本任务书第 4 节勾掉。

## 4. 完成后

- [x] 三个工具注册成功且可被 MCP 客户端调用
- [x] live 冒烟通过且测试数据已清理（dev 无残留 case/doc）
- [x] pytest 全绿
- [x] RELEASE.md 版本登记
- [x] 本文件第 4 节勾选 + 在 RELEASE 或 README 记一句"golden/delete 工具已实现（对应 general-rag issue #6 契约 §8 tool 4/6）"

## 5. 边界（别越界）

- **不改** search/namespaces/health/ingest 现有工具行为；**不改** rag_client 的既有方法语义（可加新方法）。
- **不动后端**（general-rag 仓库已全部上线：/golden/*、/delete 的 golden_impact 均已实现并验证，勿在那边重复开发）。
- 不加"自动确认"逻辑：写操作确认永远在调用方（agent-用户对话）完成，工具只执行与如实返回。
- 不加库外超纲问法测试集。
