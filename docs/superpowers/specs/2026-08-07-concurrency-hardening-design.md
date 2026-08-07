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
4. `_unmount_one` 连接池泄漏（代码自留 TODO，靠 GC）
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

- Stream：`audit:calls`，MAXLEN ~50000（approximate），消费者组 `calls-consumers`，消费者名 = 容器 hostname
- proxy 删除 `db.py`/`record_call`（不再直连 MySQL）
- 失败行 message/journey 完整（前端失败面板依赖）；成功行留空（现状等价）
- 落库延迟 <1s（XREADGROUP block 1s + batch 100）

## 第 2 节：gateway-proxy 并发加固

### 2.1 Token 本地缓存
- 进程内 TTL 缓存：`token_hash → token_info`，TTL 60s，LRU 上限 ~1000
- admin 改 token → publish `token:changed` → proxy 失效对应项（扩 pubsub 订阅）
- Redis 瞬时故障 → 缓存继续放行，防 403 风暴
- 命中缓存免 Redis，未命中走 Redis（verify_token 语义不变）

### 2.2 连接池泄漏修复（registry.py TODO）
- 存 `_mounted_clients[name] = provider` 引用，unmount 时显式 `aclose()` 底层 client

### 2.3 watch_changes pubsub 自愈
- 断线重建订阅（aclose → pubsub() → subscribe），与搜索 MCP `_resubscribe` 对齐

### 2.4 后端背压
- per-backend semaphore（默认 100）+ 调用总超时（默认 30s）

### 2.5 调用总超时
- `call_next` 包 `asyncio.wait_for`，超时 → ToolError(TimeoutError)，计入审计

### 2.6 XADD 失败兜底
- 日志 + 指标（audit_dropped_total），不重试

## 第 3 节：搜索 MCP 并发加固（tavily 样本，brave/serpapi 同构）

### 3.1 httpx client 复用
- 全局单例 `httpx.AsyncClient`（连接池，默认 limits 100）
- key 请求级 `headers={"Authorization": f"Bearer {key}"}`，不绑 client
- 共享 client **禁止设默认 Authorization 头**（R5：防 key 串用）
- `TavilyClient` 改薄封装：持有共享 client + 每请求传 key；公开方法签名不变
- `_call_with_pool` 的 `factory(key, timeout)` → `factory(timeout)`，测试 FakeClient 注入不变

### 3.2 KeyPool 借用语义
- in-flight 计数：选择时临时递减 remaining，完成归还
- `asyncio.Lock` 保护选择+记账段（单实例内足够）
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

规范分三类写清（见 6.1 C1）。

## 第 5 节：测试与部署

### 测试
- 单测：token 缓存/失效/Redis 降级；XADD 成败；semaphore 排队；超时 ToolError；unmount 显式关闭；消费者 XREADGROUP→executemany→XACK；batch 失败→死信；KeyPool 借用分散；pipeline；退避
- 压测：httpx 并发打 gateway /mcp，测 100/500/1000 并发；断言审计落库 <2s、无 403 风暴、无单 key 打爆
- 回归：现有 pytest 全绿 + smoke_test + admin UI 手工验

### 部署与回滚（生产 10.33.17.72）
1. 本地全测试过 → commit
2. SSH 拉代码 → `bash deploy.sh`
3. **渐进删双写**（R4）：`audit:failures` 保留 1-2 周，消费者稳定后删
4. 验证：compose ps 全 UP / 请求日志有数据 / 失败面板有轨迹 / Dashboard 正常（p95 含排队时间，预期变化）/ metrics 正常
5. 回滚：git 回退 commit + redeploy（双写逻辑还原）

### 新增可观测性指标
- `audit_queue_depth`（stream 滞后）
- `audit_dropped_total`（XADD 失败）
- `token_cache_hit_total` / `token_cache_miss_total`
- `audit_batch_size` / `audit_batch_latency`

## 第 6 节：开发规范沉淀（硬性交付物）

### 6.1 templates/mcp-template/CLAUDE.md — 新 MCP 必读

新增「并发与性能规范」章节（强制检查项）：

```
### C1. HTTP client 必须复用
- 禁止每调用新建 httpx.AsyncClient
- 三种正确形态：单后端单例（zabbix）/ 多 key 共享+请求级 headers（搜索）/ SDK 账户缓存（aliyun）
- 共享 client 禁止设默认 Authorization 头

### C2. 外呼必须有超时
- 默认 5s，长任务单独放宽；长任务 semaphore ≤5

### C3. 幂等重试必须带退避
- 429/限流指数退避；非幂等禁止自动重试

### C4. Redis 每请求往返必须合并
- 热点路径 pipeline；禁止每请求 3+ 次独立命令

### C5. 共享状态必须考虑并发
- 单实例锁 + 借用语义；多实例需 Redis 原子操作

### C6. pubsub 监听必须自愈
- 断线重建订阅，禁止"断了只能重启容器"
```

### 6.2 gateway-proxy/CLAUDE.md
- 审计数据流：proxy 只写 `audit:calls`，禁止直连 MySQL
- token 缓存 + 失效订阅 + Redis 降级
- 背压/超时配置项
- pubsub 自愈规范
- unmount 必须显式关闭 client

### 6.3 gateway-admin/CLAUDE.md
- 审计消费者：XREADGROUP 批量 + 死信 + 恢复
- 查询只读 MySQL calls 表，禁读 Redis stream

### 6.4 根 CLAUDE.md
- MCP 开发规范速查表补并发条目（C1-C6 一行摘要）

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

## 范围外（明确不做）

- 抽 mcp-common 共享包（方案 C）：需改 5 个已部署 MCP，违反约束；留作后续独立 spec
- 引入独立 MQ（Kafka）：千级 QPS 不需要，Redis Stream 足够
- 网关多实例/横向扩容：单实例生产，mount 状态在进程内存；后续独立 spec
- calls 表保留策略/归档：独立问题，后续 spec
