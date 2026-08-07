# 并发加固设计 — Gateway + 后端 MCP server

日期：2026-08-07
状态：已确认（brainstorming 完成）
目标：10-50 QPS → 千级 QPS，**不影响现有功能与调用方式**

## 背景

分析发现 gateway-proxy 与后端 MCP server 存在多并发瓶颈：

### gateway-proxy
1. 审计同步写 MySQL（每请求 1 次 INSERT）在请求热路径，池 maxsize=10
2. 失败路径 Redis stream + MySQL 双写（stream 已无读取方 = 死重）
3. token 每请求 Redis hgetall，Redis 抖 = 全站 403 风暴
4. **`_get_client()` 每请求新建 Client + `httpx2.AsyncClient` 连接池**（proxy.py:822 + _httpx_utils.py:95，已核实）——每请求到后端 MCP 一次 TCP+TLS 握手；`_unmount_one` 另留连接池泄漏 TODO（靠 GC）
5. `watch_changes` pubsub 断连无自愈（搜索 MCP 已修，gateway 未修）
6. 无背压、无调用总超时

### 搜索 MCP（tavily/brave/serpapi）
1. 每调用新建 + 关闭 httpx.AsyncClient（零连接复用，每请求 TCP+TLS 握手）
2. KeyPool 无借用语义，并发撞同一 key → 429 风暴
3. on_success 每请求 3 次 Redis 往返
4. 无并发上限，429 重试无退避

### 已确认正确（读代码核实，非假设）
- zabbix：进程级单例 client，httpx 复用 ✅
- aliyun：ClientFactory 按账户缓存 SDK client + to_thread + throttled 退避 ✅

## 设计决策（brainstorming 确认）

| # | 决策 |
|---|---|
| D1 | 审计经 Redis Stream 缓冲，MySQL 落库移出 proxy 请求路径 |
| D2 | 消费者放 gateway-admin（lifespan 后台 task），非 proxy 非独立容器 |
| D3 | 统一单流 `audit:calls`（成功+失败），删除 `audit:failures` 双写 |
| D4 | 审计可丢、请求优先；XADD 失败仅日志+指标，不重试 |
| D5 | 搜索 MCP 改造（client 复用/借用/pipeline/退避），zabbix/aliyun 只沉淀规范 |
| D6 | 本地测试+压测通过后部署生产 10.33.17.72 |
| D7 | 规范沉淀为硬性交付物：templates + 各 CLAUDE.md + 根 CLAUDE.md + knowledge-base 重构 |

## 第 1 节：审计数据流

```
MCP Client → gateway-proxy
               │  ① tools/call
               ├── ② XADD audit:calls（成功/失败全量，含 message/journey）
               │     ← 队满 MAXLEN 截断；失败不阻断请求
               └── ③ 返回响应（不再依赖 MySQL）
                         ▲
gateway-admin（消费者，lifespan 后台 task）
               │  ④ XREADGROUP audit:calls batch=100, block=1s
               │  ⑤ executemany 批量 INSERT calls 表
               │  ⑥ XACK
               └── ⑦ 死信：连续 N 次失败的 batch 移入 audit:calls:dead
```

- Stream：`audit:calls`（新流，替代 `audit:failures` 10000 上限的旧流），MAXLEN 50000（approximate），消费者组 `calls-consumers`，消费者名 = 容器 hostname
- **middleware.py 合并**：现有 `record_call_failure`（Redis 流）+ `record_call_audit`（MySQL）两个审计函数合并为**一次 XADD**（成功/失败同一入口，message/journey 按 status 区分）——实现者须同时删这两个函数及其调用点，不能只删 audit.py
- proxy 删除 `db.py`（唯一调用方是 audit.py，已核实）
- 失败行 message/journey 完整（前端失败面板依赖）；成功行留空（现状等价）
- 落库延迟 <1s（XREADGROUP block 1s + batch 100）
- **time 格式保持不变**：现有 proxy 写 `%Y-%m-%d %H:%M:%S.000`（middleware.py:157，固定 .000 无真实毫秒）。stream 消息沿用同格式，消费者原样写入 DATETIME(3) 列——**禁止"顺手加毫秒"**：dashboard 时间桶按 `%Y-%m-%d %H:%M:%S` 切分（dashboard.py:118-131），加毫秒破坏桶匹配
- **journey 列默认值**：schema 是 `DEFAULT ('[]')`（01_calls.sql:16），消费者必须显式写 journey（成功行 `[]`，失败行完整 JSON），不能依赖 DB 默认值——stream 消息里 journey 恒存在

## 第 2 节：gateway-proxy 并发加固

### 2.1 Token 本地缓存
- 进程内 TTL 缓存：`token_hash → token_info`，TTL 60s，LRU 上限 ~1000
- Redis 瞬时故障 → 缓存继续放行，防 403 风暴
- 命中缓存免 Redis，未命中走 Redis（verify_token 语义不变）
- **缓存失效必须新增通道**：admin tokens.py 当前 create/delete **不 publish 任何通知**（已核实，tokens.py 仅 hset/set）。删除 token 后缓存仍可用 60s = 吊销延迟（安全漏洞）。
  - **新增**：admin `tokens.py` 在 create/delete 时 publish `token:changed`（payload 含 token_hash — delete 接口只有 token_id，须先查 `token_id:{token_id}` 拿 token_hash，tokens.py:95 已有此查询）
  - **新增**：proxy `watch_changes` 扩订阅 `token:changed` → 失效对应缓存项
  - **订阅实现**：复用现有 `watch_changes` 的**同一条** pubsub 连接订阅双频道（`server:changed` + `token:changed`，redis-py 支持多频道 subscribe），listen 循环按 `msg["channel"]` 分流处理 — 自愈逻辑只维护一个连接，避免第二条 pubsub 连接带来双倍断线面
  - 权限变更（update 权限）同走 `token:changed`，保证吊销与变更即时生效

### 2.2 Proxy client 复用 + 连接池泄漏修复（registry.py TODO 升级）
- **背景（已核实，三轮自审最深发现）**：`create_proxy()` 默认 client_factory 每次 `_get_client()` 都新建 Client（proxy.py:822-824 工具调用路径），transport 内部 `create_mcp_http_client` 每次都新建 `httpx2.AsyncClient`（_httpx_utils.py:95）——**每请求到后端 MCP 都是新 TCP+TLS 连接**，与搜索 MCP 的 client 新建问题同构。原 spec 2.2 只提 unmount 泄漏，漏了 hot path 每请求新建
- **改造**：`create_proxy` 改为传入**复用 client_factory**（FastMCPProxy 支持 client_factory 参数，proxy.py:1249 文档明确"gives you full control over session creation and reuse"）——client_factory 返回**缓存的 Client**（按后端 URL 缓存，复用底层 transport 连接池）
- 存 `_mounted_clients[name] = client_factory` 引用，unmount 时显式关闭缓存 Client 的 transport（`aclose`），不再依赖 GC
- 可行性：FastMCP 的 `ProxyClient.new()`（proxy.py:1179）支持派生连接复用——实现时优先用官方派生机制，次选自建缓存

### 2.3 watch_changes pubsub 自愈
- 断线重建订阅（aclose → pubsub() → subscribe），与搜索 MCP `_resubscribe` 对齐

### 2.4 后端背压
- per-backend semaphore（默认 100），排队请求计入等待时间（R6：latency 含排队，p95 反映真实端到端）
- 总超时见 2.5（默认 90s，非 30s）

### 2.5 调用总超时
- `call_next` 包 `asyncio.wait_for`，超时 → ToolError(TimeoutError)，计入审计
- **默认值必须 ≥ 后端最长任务超时**：tavily crawl/research 是 60s（LONG_TASK_TIMEOUT，已核实）——proxy 总超时默认 30s 会杀死长任务，违反"不影响现有功能"
- 修正：总超时默认 **90s**（后端最长 60s + 余量），支持 per-server 覆盖（`servers:{name}` hash 加 `call_timeout` 字段，admin 可配）
- 配置缺失时回退默认 90s
- **admin 侧需改动**：`ServerCreate`/`ServerUpdate` model 加 `call_timeout: float | None = None` 字段（现有仅 url/description，已核实；None → proxy 用默认 90s，向后兼容，不影响现有注册流程）

### 2.6 XADD 失败兜底
- 日志 + 指标（audit_dropped_total），不重试

## 第 3 节：搜索 MCP 并发加固（tavily 样本，brave/serpapi 同构）

### 3.1 httpx client 复用
- 全局单例 `httpx.AsyncClient`（连接池，默认 limits 100）
- key 请求级 `headers={"Authorization": f"Bearer {key}"}`，不绑 client
- 共享 client **禁止设默认 Authorization 头**（R5：防 key 串用）
- `TavilyClient` 改薄封装：内部用共享 client，**公开方法签名不变**（`search(params)` 等），每请求传 key 头 + 每请求 timeout（httpx 支持 per-request timeout）
- **`factory(key, timeout)` 签名保持不变**（仅内部不再新建 AsyncClient）——改签名会波及全部测试 FakeClient，违反"不影响调用方式"约束
- `_call_with_pool` 仅内部实现变更，测试 FakeClient 注入不变

### 3.2 KeyPool 借用语义
- in-flight 计数：选择时临时递减 remaining，完成归还
- `asyncio.Lock` 保护选择瞬间（next_key 内部）+ 记账瞬间（on_success/on_error 内部）+ **reload() 整表替换**（防止热更新与 in-flight 记账竞态：reload 换新 `_records` dict，on_success 持旧 rec 写回 → 字段更新丢失，已核实 key_pool.py:136-152 整表替换）——**锁绝不包外呼 await**（否则所有并发请求持锁等 API，串行化背压失效）
- 借用语义落地：next_key 选择 key 时把 remaining 临时扣减 in-flight 值（后续请求自然选到别的 key），完成/失败后 on_success/on_error 归还
- 边界：多实例部署需 Redis 原子借出（Lua），留作演进（D5 单实例生产）

### 3.3 Redis 往返合并
- on_success 的 hset+zadd+expire 三连 → pipeline 一次往返

### 3.4 并发上限 + 退避
- per-endpoint semaphore（search/extract/map=20，crawl/research=5）
- 429 重试指数退避（0.5s/1s），冷却 key 不立即重打

## 第 4 节：zabbix/aliyun 适配（只沉淀规范）

| MCP | client 形态 | 需改造？ |
|---|---|---|
| zabbix | 进程级单例 client，httpx 复用 | 否（正面样板） |
| aliyun | ClientFactory 账户缓存，SDK 复用 | 否（正面样板） |

补充（读码核实）：
- aliyun 已有自身 pubsub 自愈（account_store.py `_resubscribe` 逻辑）+ `aliyndns:changed` 通道已存在 — 与 spec 2.3 的 proxy 自愈改造无关，aliyun 无需动
- zabbix 无 key 池、无 pubsub — 不涉及热更新通道

规范分三类写清（见 6.1 C1）。

## 第 5 节：测试与部署

### 测试
- 单测：token 缓存/失效/Redis 降级；XADD 成败；semaphore 排队；超时 ToolError；unmount 显式关闭；消费者 XREADGROUP→executemany→XACK；batch 失败→死信；KeyPool 借用分散；pipeline；退避
- 单测可行性（已核实）：fakeredis 支持 XADD/XGROUP_CREATE/XREADGROUP/XACK 全部 stream 操作，消费者逻辑可纯内存测试，无需真 Redis
- 压测：httpx 并发打 gateway /mcp，测 100/500/1000 并发；断言 stream 写入无失败、无 403 风暴（Redis 抖时不崩）、无单 key 打爆（429 率不飙升）
- **端到端审计延迟验证需真环境**：本地压测无 admin 消费者进程，"落库 <2s"断言改为二阶段——(a) 本地压测验证 XADD 成功率与 proxy 延迟（MySQL 已移出路径，proxy 延迟不依赖存储）；(b) 部署生产后验证 stream→MySQL 落库延迟（消费者真实运行）
- 回归：现有 pytest 全绿 + smoke_test + admin UI 手工验

### 部署与回滚（生产 10.33.17.72）
1. 本地全测试过 → commit
2. SSH 拉代码 → `bash deploy.sh`
3. **部署顺序（审计断档防护）**：compose 同时重建 proxy+admin 时启动顺序不保证（compose 只保证依赖序，proxy 不依赖 admin）。proxy 先切 stream 写、admin 消费者未起 → 审计断档。缓解二选一：
   - (a) 分两次：先 `docker compose up -d gateway-admin`（消费者先起），再 `gateway-proxy`（切写入）——stream 有 MAXLEN 缓冲，admin 先起后 proxy 写入即被消费，零断档
   - (b) 接受一次性断档（窗口 = admin 重启秒级），风险低（审计可丢 D4）
   推荐 (a)，spec 采用
4. **渐进删双写**（R4）：`audit:failures` 保留 1-2 周，消费者稳定后删
5. 验证：compose ps 全 UP / 请求日志有数据 / 失败面板有轨迹 / Dashboard 正常（p95 含排队时间，预期变化）/ metrics 正常
6. 回滚：git 回退 commit + redeploy（双写逻辑还原，calls 表继续由 proxy 直写 — 改造期间双写保留正是为此）

### 新增可观测性指标
- `audit_queue_depth`（stream 滞后）
- `audit_dropped_total`（XADD 失败）
- `token_cache_hit_total` / `token_cache_miss_total`
- `audit_batch_size` / `audit_batch_latency`

## 第 6 节：开发规范沉淀（硬性交付物）

### 6.1 templates/mcp-template/CLAUDE.md — 新 MCP 必读

新增「并发与性能规范」章节（强制检查项），**并同步修订现有冲突章节**：

```
### C1. HTTP client 必须复用
- 禁止每调用新建 httpx.AsyncClient
- 三种正确形态：单后端单例（zabbix）/ 多 key 共享+请求级 headers（搜索）/ SDK 账户缓存（aliyun）
- 共享 client 禁止设默认 Authorization 头

### C2. 外呼必须有超时
- 默认 5s，长任务单独放宽；长任务 semaphore ≤5

### C3. 幂等重试必须带退带
- 429/限流指数退避；非幂等禁止自动重试

### C4. Redis 每请求往返必须合并
- 热点路径 pipeline；禁止每请求 3+ 次独立命令

### C5. 共享状态必须考虑并发
- 单实例锁 + 借用语义；多实例需 Redis 原子操作

### C6. pubsub 监听必须自愈
- 断线重建订阅，禁止"断了只能重启容器"
```

**同步修订（审模板发现，防自相矛盾）**：
- **§5 出网代理示范必须改**：现有 `client = httpx.AsyncClient(timeout=10, proxy=proxy)`（每调用新建）正是 C1 禁止的模式 — 改为共享 client 单例 + proxy 配置
- **C1 与现有章节位置融合**：三种形态分别落位 — 单后端单例 → §1.5 工具组织模式；多 key 共享 → §6 多 API key 池；SDK 账户缓存 → §2.5 附近（新 MCP 按需取用，不重复建独立章节）
- **C5 与 §2 key 安全合并表述**：共享 client 防 key 串用（禁默认 Authorization 头）与现有"明文 key 禁入日志/metric"同属 key 安全主题，合并表述避免散落
- §5 代理示范修正为共享 client 形态

### 6.2 gateway-proxy/CLAUDE.md
- **必须改现有行（审模板发现自相矛盾源）**：line 11 "失败同时双写 Redis Stream（audit:failures，仅作回滚兜底）" → 改为 "全量调用（成功+失败）XADD 至 audit:calls stream，MySQL 落库在 admin 消费者；proxy 不直连 MySQL"
- 审计数据流：proxy 只写 `audit:calls`，禁止直连 MySQL
- token 缓存 + 失效订阅 + Redis 降级
- 背压/超时配置项
- pubsub 自愈规范
- unmount 必须显式关闭 client

### 6.3 gateway-admin/CLAUDE.md
- **必须改现有行**：line 10 "Redis 仅 servers/tokens/失败双写兜底" → 改为 "Redis 仅 servers/tokens；审计消费者 XREADGROUP 批量落 MySQL"
- 审计消费者：XREADGROUP 批量 + 死信 + 恢复
- 查询只读 MySQL calls 表，禁读 Redis stream

### 6.4 根 CLAUDE.md
- MCP 开发规范速查表补并发条目（C1-C6 一行摘要）
- **同步修订 5 处旧双写描述**（审文档发现，改造后全部失实）：
  - line 38 "Redis（配置/状态）"（架构图）— 已正确，不动
  - line 42 "audit:failures（失败流）" → 删/改 "audit:calls（全量审计缓冲流）"
  - line 46 "调用审计写 MySQL" → "调用审计 XADD audit:calls，MySQL 落库在 admin 消费者"
  - line 50 "失败审计流" → "全量审计缓冲流"
  - line 121 端口表 "配置/状态/失败审计" → "配置/状态/审计缓冲"

### 6.5 knowledge-base 目录重构 + 新模式文档

```
knowledge-base/
├── README.md              # 索引升级：分三类 + 场景触发表
├── patterns/              # 可复用设计模式（新目录）
│   ├── search-mcp-key-pool-pattern.md        # 从根移入
│   ├── mcp-account-level-permission-pattern.md
│   └── audit-async-stream-pattern.md         # 本次新增
├── pitfalls/              # 踩坑记录（新目录）
│   └── mcp-production-deployment-pitfalls.md
└── fastmcp-v4/            # 官方文档（不动）
```

- patterns/（可复用模式）vs pitfalls/（一次性教训）分类
- 纯文件迁移，更新 README + 根 CLAUDE.md 引用路径
- **templates/mcp-template/CLAUDE.md 3 处引用同步改**（审文档发现，漏改则新 MCP 链接失效）：line 51/52/53 知识库节 + line 211 key 池节 → `knowledge-base/patterns/search-mcp-key-pool-pattern.md` 等新路径
- README 升级为场景触发表（什么情况查什么），非简单清单
- `audit-async-stream-pattern.md`：审计异步化模式（stream 缓冲 + 消费者批量落库 + 死信），含 D1-D4 决策与 R4/R5/R6 权衡

## 风险清单

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 审计延迟变秒级（失败面板"实时"变"准实时"） | 接受；<1s 无感，决策写进 spec |
| R2 | admin 挂 → 审计堆积，MAXLEN 截断丢最老 | XREADGROUP pending 恢复续读；审计可丢（D4） |
| R3 | 单消费者组，admin 多实例抢批 | 预留消费者组语义，多实例自动负载均衡；生产单实例 |
| R4 | 删双写 → 回滚路径变窄 | 渐进：保留 audit:failures 1-2 周再删 |
| R5 | 共享 client key 串用 | 共享 client 禁默认 Authorization 头 + 规范 C1 强制 |
| R6 | 排队时间计入 latency，p95 变化 | 期望行为（真实端到端），写进 spec/规范 |
| R7 | KeyPool 借用锁仅单实例有效 | 单实例足够；多实例需 Redis 原子借出，写边界 |
| R8 | Redis 故障全栈降级 | 现有架构固有，token 缓存撑 60s |
| R9 | MAXLEN 截断粒度 | 50000 条 = 千级 QPS 下 50s 缓冲；仅 admin 长挂触发 |
| R10 | FastMCP beta 升级风险 | 锁版本 4.0.0b1，无即时风险 |
| R11 | client_factory 复用改造引入回归（默认新建→复用，session 语义变化） | ProxyClient.new() 派生机制（proxy.py:1179）是官方支持路径；改造后全量回归 + 压测验证工具调用正确性；回滚即恢复默认 factory |

## 范围外（明确不做）

- 抽 mcp-common 共享包（方案 C）：需改 5 个已部署 MCP，违反约束；留作后续独立 spec
- 引入独立 MQ（Kafka）：千级 QPS 不需要，Redis Stream 足够
- 网关多实例/横向扩容：单实例生产，mount 状态在进程内存；后续独立 spec
- calls 表保留策略/归档：独立问题，后续 spec
