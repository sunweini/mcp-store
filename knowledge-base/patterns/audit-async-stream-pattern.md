# 审计异步化模式（Redis Stream 缓冲 + 消费者批量落库）

## 适用场景
网关/中间层需要全量调用审计，但同步写存储拖慢请求路径（QPS 千级时 MySQL INSERT 成瓶颈）。

## 架构
proxy 只 XADD `audit:calls` stream（MAXLEN 50000，成功+失败全量）→ 消费者（独立进程/lifespan task）XREADGROUP batch=100/block=1s → executemany 批量 INSERT → XACK；落库失败 batch 即移 `audit:calls:dead` 死信流（无重试累积）。

## 关键决策（D1-D4）
- 审计可丢、请求优先：XADD 失败仅日志+指标，不重试（R4）
- 落库延迟 <1s（block 1s + batch 100），失败面板"实时"变"准实时"（R1）
- 消费者挂 → stream 堆积 + MAXLEN 截断丢最老；XREADGROUP pending 恢复续读（R2）
- time 格式锁死（禁止顺手加毫秒——下游按秒切桶）

## 部署顺序（审计断档防护）
先起消费者进程（admin），再切 proxy 写入——stream 缓冲，零断档。
