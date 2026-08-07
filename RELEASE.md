# 版本更新说明

本仓库版本更新记录。每个里程碑记录：变更内容、影响范围、升级/部署注意事项、回滚方式。

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
