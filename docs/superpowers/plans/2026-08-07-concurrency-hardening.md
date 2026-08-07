# 并发加固实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 MCP Gateway + 后端搜索 MCP 从 10-50 QPS 加固到千级 QPS，不影响现有功能与调用方式。

**Architecture:** 审计从"proxy 同步写 MySQL + Redis 双写"改为"proxy 只 XADD audit:calls stream，gateway-admin 消费者批量落 MySQL"；proxy 加 token 本地缓存、Client 复用、背压超时、pubsub 自愈；tavily/brave/serpapi 改 httpx client 复用 + KeyPool 借用语义 + pipeline + 退避；最后将全部并发规范沉淀到 templates 与各 CLAUDE.md。

**Tech Stack:** Python 3.12 / FastMCP 4.0.0b1 / httpx / redis.asyncio / aiomysql / fakeredis / pytest / OTel

## Global Constraints

- **测试基线（实施前必须全绿）**：gateway-proxy 76 passed / tavily-mcp 63 passed / gateway-admin 120 passed
- FastMCP 锁版本 `4.0.0b1`，MCP Protocol `2026-07-28`，stateless HTTP
- 包管理 uv（`--prerelease=allow`），uv.lock 必须阿里云镜像（`UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ uv lock`）
- 可观测性遵循 `~/.claude/docs/observability-coding-standards.md`：结构化日志、OTel span、低基数 label
- 注释写"为什么"不写"做了什么"（OBS-CORE-005）
- key_id 与明文 API key 禁止入日志/metric label
- **time 格式锁死**：审计 time 恒为 `%Y-%m-%d %H:%M:%S.000`（禁止加毫秒，破坏 dashboard 桶匹配）
- **共享 client 禁止设默认 Authorization 头**（R5 key 串用防护）
- 部署顺序：先 gateway-admin 后 gateway-proxy（审计断档防护）

---

### Task 1: gateway-proxy 审计写入改为 XADD audit:calls

**Files:**
- Modify: `gateway-proxy/audit.py`（整体重写）
- Modify: `gateway-proxy/middleware.py`（record_call_failure/record_call_audit 合并）
- Modify: `gateway-proxy/observability.py`（加 audit_dropped_total）
- Delete: `gateway-proxy/db.py`
- Test: `gateway-proxy/tests/test_audit.py`（改写）、`gateway-proxy/tests/test_record_call_failure.py`（改写）

**Interfaces:**
- Consumes: 现有 `get_redis()`（redis_client.py）
- Produces: `record_call_stream(meta: dict, status: str, error_type: str | None, message: str | None, journey: list) -> None` — 单次 XADD，成功/失败同一入口

- [ ] **Step 1: 写失败测试 — audit.py 改为单 XADD 入口**

改写 `tests/test_audit.py`：

```python
async def test_record_call_stream_xadds_success(fake_redis):
    from audit import record_call_stream
    await record_call_stream(
        meta={"time": "2026-08-07 12:00:00.000", "server": "tavily-mcp", "tool": "tavily_search",
              "op": "read", "token_name": "test", "latency_ms": 5, "trace_id": "t1"},
        status="ok", error_type=None, message="", journey=[],
    )
    entries = await fake_redis.xrange("audit:calls", count=1)
    assert len(entries) == 1
    msg = entries[0][1]
    assert msg["server"] == "tavily-mcp"
    assert msg["status"] == "ok"
    assert msg["journey"] == "[]"
    assert msg["time"] == "2026-08-07 12:00:00.000"  # 格式锁死，无毫秒精度变化
```

- [ ] **Step 2: 跑测试验证失败**

Run: `cd gateway-proxy && uv run pytest tests/test_audit.py -q`
Expected: FAIL — `from audit import record_call_stream` ImportError

- [ ] **Step 3: 重写 audit.py — 单 XADD 入口**

```python
"""Failure audit: proxy 只 XADD audit:calls stream，MySQL 落库在 admin 消费者。

改造前 proxy 同步写 MySQL calls 表 + Redis audit:failures 双写；现在 MySQL
完全移出请求路径（D1/D3）——单流 audit:calls 承载成功+失败全量，消费者
（gateway-admin）XREADGROUP 批量落库。XADD 失败仅日志+指标（D4 审计可丢）。
"""
import structlog
from redis_client import get_redis

logger = structlog.get_logger()

_STREAM = "audit:calls"
# MAXLEN trims the stream so it cannot grow unbounded (R9: 50000 条 = 千级 QPS 下 50s 缓冲)
_STREAM_MAXLEN = 50000


async def record_call_stream(
    meta: dict,
    status: str,
    error_type: str | None = None,
    message: str | None = None,
    journey: list | None = None,
) -> None:
    """Append one audit record to audit:calls stream. Never raises (D4)."""
    r = get_redis()
    try:
        await r.xadd(
            _STREAM,
            {
                "time": meta["time"],
                "server": meta["server"],
                "tool": meta["tool"],
                "op": meta["op"],
                "token_name": meta["token_name"],
                "latency_ms": str(meta["latency_ms"]),
                "status": status,
                "error_type": error_type or "",
                "message": message or "",
                "journey": __import__("json").dumps(journey or []),
                "trace": meta["trace_id"],
            },
            maxlen=_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception as e:
        # 审计绝不断请求路径；失败计入 audit_dropped_total 指标（observability 模块运行时取值）
        import observability
        if observability.AUDIT_DROPPED_TOTAL:
            observability.AUDIT_DROPPED_TOTAL.add(1, {})
        logger.error("audit_xadd_failed", error=str(e), service="gateway-proxy")
```

- [ ] **Step 4: observability.py 加 AUDIT_DROPPED_TOTAL 指标**

在 `observability.py` 模块级加 `AUDIT_DROPPED_TOTAL = None`，`init_telemetry()` 内加：

```python
AUDIT_DROPPED_TOTAL = meter.create_counter("audit_dropped_total", description="Audit stream XADD failures")
```

（`global AUDIT_DROPPED_TOTAL` 声明同步加）

- [ ] **Step 5: middleware.py 合并两个审计函数为一次 XADD**

删除 `record_call_failure` + `record_call_audit` 两函数（含 journey 构建逻辑保留在 `build_journey`），在 `on_call_tool` 的三处调用点（拒绝路径 / 异常路径 / 成功路径）替换为：

```python
# 成功路径（原 record_call_audit 调用点）
await record_call_stream(
    meta={...time/server/tool/op/token_name/latency_ms/trace_id...},
    status="ok", error_type=None, message="", journey=[],
)
# 失败路径（原 record_call_failure + record_call_audit 双写点）合并为一次
await record_call_stream(
    meta={...同字段...},
    status="fail", error_type=error_type, message=message,
    journey=build_journey(fail_stage, server, latency_ms),
)
```

meta 字段与原 record_call_audit 完全一致（time/server/tool/op/token_name/latency_ms/trace_id）。删除 `from audit import record_failure, record_call`，改 `from audit import record_call_stream`。

- [ ] **Step 6: 删除 db.py**

`rm gateway-proxy/db.py`（唯一调用方 audit.py 已改，已核实）。

- [ ] **Step 7: 改写失败审计测试**

改写 `tests/test_record_call_failure.py` — 原测 `record_failure`（Redis 流）改为测 `record_call_stream` 失败路径（message/journey 完整写 stream）：

```python
async def test_record_call_stream_fail_path(fake_redis):
    from audit import record_call_stream
    await record_call_stream(
        meta={"time": "2026-08-07 12:00:00.000", "server": "zabbix-mcp", "tool": "zabbix_list",
              "op": "read", "token_name": "t", "latency_ms": 30, "trace_id": "t2"},
        status="fail", error_type="upstream_timeout", message="timeout",
        journey=[{"stage": "auth", "state": "fail", "ms": 30}],
    )
    entries = await fake_redis.xrange("audit:calls", count=1)
    msg = entries[0][1]
    assert msg["status"] == "fail"
    assert msg["error_type"] == "upstream_timeout"
    assert msg["message"] == "timeout"
    assert '"stage": "auth"' in msg["journey"]
```

- [ ] **Step 8: 全量回归**

Run: `cd gateway-proxy && uv run pytest tests/ -q`
Expected: 全绿（原 76 通过，新增 XADD 测试通过；原测 MySQL 写入的测试同步删除）

- [ ] **Step 9: Commit**

```bash
git add gateway-proxy/audit.py gateway-proxy/middleware.py gateway-proxy/observability.py gateway-proxy/tests/
git rm gateway-proxy/db.py
git commit -m "feat(gateway-proxy): 审计改单流 XADD audit:calls，MySQL 移出请求路径，删 db.py"
```

---

### Task 2: gateway-admin 审计消费者（XREADGROUP 批量落库 + 死信）

**Files:**
- Create: `gateway-admin/audit_consumer.py`
- Modify: `gateway-admin/app.py`（lifespan 挂消费者 task）
- Test: `gateway-admin/tests/test_audit_consumer.py`

**Interfaces:**
- Consumes: `get_pool()`（db.py 现有，保留）、`get_redis()`（redis_client.py 现有）
- Produces: `start_consumer() -> asyncio.Task`（lifespan 调用）、`stop_consumer(task)`（shutdown 调用）

- [ ] **Step 1: 写失败测试 — 消费者批量落库 + XACK**

```python
async def test_consumer_batch_inserts_and_acks(fake_redis, monkeypatch):
    """XREADGROUP 拉取 → executemany INSERT → XACK 全链路。"""
    import audit_consumer
    fake_pool = FakePool()
    monkeypatch.setattr(audit_consumer, "get_pool", lambda: fake_pool.get())
    # 造 3 条 stream 消息
    for i in range(3):
        await fake_redis.xadd("audit:calls", {
            "time": "2026-08-07 12:00:00.000", "server": "tavily-mcp", "tool": "tavily_search",
            "op": "read", "token_name": "t", "latency_ms": "5", "status": "ok",
            "error_type": "", "message": "", "journey": "[]", "trace": f"tr{i}",
        })
    await fake_redis.xgroup_create("audit:calls", "calls-consumers", id="0")
    await audit_consumer._consume_batch(fake_redis)
    assert fake_pool.inserted == 3
    # XACK 已确认
    pending = await fake_redis.xpending("audit:calls", "calls-consumers")
    assert pending["pending"] == 0
```

（FakePool 实现：`get()` 返回 fake conn，`cursor()` 记录 `executemany` 行数到 `inserted`，`commit()` no-op。参照 admin tests 现有 fakeredis conftest 模式。）

- [ ] **Step 2: 跑测试验证失败**

Run: `cd gateway-admin && uv run pytest tests/test_audit_consumer.py -q`
Expected: FAIL — `import audit_consumer` ModuleNotFoundError

- [ ] **Step 3: 写 audit_consumer.py — 批量消费 + 死信**

```python
"""审计消费者：XREADGROUP audit:calls → executemany 批量 INSERT calls 表 → XACK。

D2：消费者放 gateway-admin（lifespan 后台 task），非 proxy 非独立容器。
批量参数：batch=100, block=1s（落库延迟 <1s，R1）。
失败语义（D4 审计可丢）：batch 落库失败 → 移入 audit:calls:dead 死信流
（XADD 一条含原始 batch + 错误信息），XACK 原消息防无限重试（R2 恢复靠
XREADGROUP last-delivered 续读，死信人工/后续处理）。
"""
import asyncio
import json
import os
import structlog

from redis_client import get_redis
from db import get_pool

logger = structlog.get_logger()

_STREAM = "audit:calls"
_GROUP = "calls-consumers"
_DEAD_STREAM = "audit:calls:dead"
_BATCH = 100
_BLOCK_MS = 1000
_DEAD_RETRIES = 3  # 连续 N 次 batch 失败进死信


def _consumer_name() -> str:
    return os.environ.get("HOSTNAME", "admin-consumer")


async def _insert_calls(rows: list[dict]) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                "INSERT INTO calls (time, server, tool, op, token_name, latency_ms, "
                "status, error_type, trace, message, journey) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(
                    r["time"], r["server"], r["tool"], r["op"], r["token_name"],
                    int(r["latency_ms"]), r["status"], r["error_type"],
                    r["trace"], r["message"], r["journey"],
                ) for r in rows],
            )


async def _move_to_dead(redis, msg_ids: list[tuple[str, str]], error: str) -> None:
    """落库失败 batch 移死信（含原始消息），供人工/后续处理。"""
    await redis.xadd(_DEAD_STREAM, {
        "batch_ids": json.dumps([i for i, _ in msg_ids]),
        "error": error,
    }, maxlen=10000, approximate=True)


async def _consume_batch(redis) -> int:
    """拉一批 → 落库 → XACK；落库失败连续 _DEAD_RETRIES 次后移死信。"""
    try:
        msgs = await redis.xreadgroup(
            _GROUP, _consumer_name(), {_STREAM: ">"}, count=_BATCH, block=_BLOCK_MS)
    except Exception as e:
        # 组不存在（首启）：创建组，下轮再读
        if "no such key" in str(e).lower() or "group" in str(e).lower():
            await redis.xgroup_create(_STREAM, _GROUP, id="0", mkstream=True)
        logger.warning("audit_consume_group_init", error=str(e), service="gateway-admin")
        return 0
    if not msgs:
        return 0
    rows, ids = [], []
    for _stream, entries in msgs:
        for mid, fields in entries:
            rows.append(fields)
            ids.append((mid, _stream))
    try:
        await _insert_calls(rows)
        await redis.xack(_STREAM, _GROUP, *[i for i, _ in ids])
        return len(ids)
    except Exception as e:
        await _move_to_dead(redis, ids, str(e))
        logger.error("audit_batch_failed", error=str(e), batch=len(ids), service="gateway-admin")
        return -len(ids)


async def _run_consumer() -> None:
    r = get_redis()
    # 确保流与组存在（幂等）
    try:
        await r.xgroup_create(_STREAM, _GROUP, id="0", mkstream=True)
    except Exception:
        pass  # 组已存在
    consecutive_failures = 0
    while True:
        n = await _consume_batch(r)
        if n < 0:
            consecutive_failures += 1
            if consecutive_failures >= _DEAD_RETRIES:
                consecutive_failures = 0  # 已移死信，重置；继续消费新消息
        elif n == 0:
            await asyncio.sleep(0.1)  # 空批：XREADGROUP block 已等 1s，极小 sleep 防忙转
```

- [ ] **Step 4: app.py lifespan 挂消费者**

```python
@asynccontextmanager
async def lifespan(app):
    await ensure_default_admin()
    import audit_consumer
    consumer_task = asyncio.create_task(audit_consumer._run_consumer())
    logger.info("admin_started", service="gateway-admin")
    yield
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass
    from redis_client import close_redis
    await close_redis()
    logger.info("admin_stopped", service="gateway-admin")
```

（注意：app.py 顶部需 `import asyncio`。消费者 task 引用保存在 lifespan 局部变量即可 — 单 worker 容器（Dockerfile CMD 无 --workers），task 存活于事件循环。）

**消费者指标（spec 新增指标落地）**：`_consume_batch` 返回后由 `_run_consumer` 记录：

```python
# observability 同款模式：admin 侧 metrics.py 已有 meter——运行时取 instrument
import metrics
if metrics.AUDIT_BATCH_SIZE:
    metrics.AUDIT_BATCH_SIZE.record(batch_size, {})
```

`_run_consumer` 内循环记录 `audit_batch_size`（本批条数）+ `audit_batch_latency`（insert 耗时，`_consume_batch` 内测时用 `time.monotonic()` 包裹 `_insert_calls`）。`audit_queue_depth` 由 `_consume_batch` 内 `await redis.xlen(_STREAM)` 取并记录（每批一次，低开销）。admin 侧 `metrics.py` 加三 instrument（检查现有 metrics.py 的 meter 模式后对齐）。

- [ ] **Step 5: 测试死信路径**

```python
async def test_consumer_dead_letter_on_failure(fake_redis, monkeypatch):
    """落库抛异常 → batch 移死信 + XACK，不无限重试。"""
    import audit_consumer
    async def _boom(rows): raise RuntimeError("db down")
    monkeypatch.setattr(audit_consumer, "_insert_calls", _boom)
    await fake_redis.xadd("audit:calls", {
        "time": "2026-08-07 12:00:00.000", "server": "tavily-mcp", "tool": "t",
        "op": "read", "token_name": "t", "latency_ms": "5", "status": "ok",
        "error_type": "", "message": "", "journey": "[]", "trace": "tr1",
    })
    await fake_redis.xgroup_create("audit:calls", "calls-consumers", id="0")
    await audit_consumer._consume_batch(fake_redis)
    dead = await fake_redis.xrange("audit:calls:dead", count=1)
    assert len(dead) == 1
    pending = await fake_redis.xpending("audit:calls", "calls-consumers")
    assert pending["pending"] == 0  # 死信后 XACK，防无限重试
```

- [ ] **Step 6: 全量回归**

Run: `cd gateway-admin && uv run pytest tests/ -q`
Expected: 全绿（原 120 通过 + 新消费者测试）

- [ ] **Step 7: Commit**

```bash
git add gateway-admin/audit_consumer.py gateway-admin/app.py gateway-admin/tests/test_audit_consumer.py
git commit -m "feat(gateway-admin): 审计消费者 XREADGROUP 批量落库 + 死信流，lifespan 挂载"
```

---

### Task 3: proxy token 本地缓存 + token:changed 失效通道

**Files:**
- Modify: `gateway-proxy/auth.py`（加缓存）
- Modify: `gateway-proxy/registry.py`（watch_changes 扩订阅 token:changed）
- Modify: `gateway-admin/api/tokens.py`（create/delete publish token:changed）
- Test: `gateway-proxy/tests/test_auth.py`（加缓存测试）、`gateway-admin/tests/test_tokens.py`（加 publish 断言）

**Interfaces:**
- Consumes: 现有 `verify_token(token) -> dict | None`（auth.py）
- Produces: `verify_token` 行为不变（缓存透明）；`invalidate_token_cache(token_hash)`（proxy 内部）；`_publish_token_changed(token_hash)`（admin 内部）

- [ ] **Step 1: 写失败测试 — 缓存命中免 Redis + 失效**

```python
async def test_verify_token_cache_hit(fake_redis):
    """第二次调用命中缓存，不触 Redis（验证 redis 调用计数）。"""
    import auth
    await auth.verify_token("tok_abc")
    # 首次未命中走 Redis；第二次命中
    auth.clear_token_cache()
    ...
```

（精确断言：monkeypatch `redis_client.get_redis` 返回带调用计数的 fake，验证第二次 verify 不触发 hgetall。）

```python
async def test_verify_token_cache_invalidate(fake_redis):
    """invalidate_token_cache 后重新走 Redis，读到新权限。"""
    await auth.verify_token("tok_abc")
    auth.invalidate_token_cache(auth.hash_token("tok_abc"))
    info = await auth.verify_token("tok_abc")
    assert info["name"] == "test"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `cd gateway-proxy && uv run pytest tests/test_auth.py -q`
Expected: FAIL — `clear_token_cache`/`invalidate_token_cache` AttributeError

- [ ] **Step 3: auth.py 加 TTL 缓存**

```python
"""Token 验证 + 本地 TTL 缓存。

高并发下每请求 Redis hgetall 是瓶颈；缓存命中免 Redis（TTL 60s，LRU 上限
1000）。Redis 瞬时故障时缓存继续放行——否则 Redis 抖 = 全站 403 风暴（R8）。
失效：admin 变更 token 后 publish token:changed → proxy watch_changes 调
invalidate_token_cache（TTL 只兜底，撤销必须即时）。
"""
import hashlib
import json
import time
from collections import OrderedDict

from redis_client import get_redis

_CACHE_TTL = 60
_CACHE_MAX = 1000
_cache: "OrderedDict[str, tuple[float, dict | None]]" = OrderedDict()


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a token string."""
    return hashlib.sha256(token.encode()).hexdigest()


def _cache_get(token_hash: str) -> dict | None:
    entry = _cache.get(token_hash)
    if entry is None:
        return None
    ts, info = entry
    if time.time() - ts > _CACHE_TTL:
        del _cache[token_hash]
        return None
    _cache.move_to_end(token_hash)  # LRU
    return info


def _cache_put(token_hash: str, info: dict | None) -> None:
    _cache[token_hash] = (time.time(), info)
    _cache.move_to_end(token_hash)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def invalidate_token_cache(token_hash: str) -> None:
    """token:changed 通道回调：吊销/变更即时失效缓存。"""
    _cache.pop(token_hash, None)


def clear_token_cache() -> None:
    """测试用：清空缓存。"""
    _cache.clear()


async def verify_token(token: str) -> dict | None:
    """Look up a token by its hash. Returns token info dict or None if invalid.

    缓存命中免 Redis；未命中走 Redis 并回填缓存。语义与改造前一致
    （invalid token 也缓存为 None，防 Redis 空查风暴——注意：token 新建
    后最长 60s 生效，可接受）。
    """
    token_hash = hash_token(token)
    cached = _cache_get(token_hash)
    if cached is not None or token_hash in _cache:
        return cached
    r = get_redis()
    data = await r.hgetall(f"tokens:{token_hash}")
    if not data:
        _cache_put(token_hash, None)
        return None
    info = {
        "id": data["id"],
        "name": data["name"],
        "permissions": json.loads(data["permissions"]),
    }
    _cache_put(token_hash, info)
    return info
```

（注意：`token_hash in _cache` 判断区分"已缓存为 None"与"未缓存"——否则 invalid token 每请求都打 Redis。）

- [ ] **Step 4: registry.py watch_changes 扩订阅 token:changed**

```python
async def watch_changes(gateway) -> None:
    """订阅 server:changed + token:changed，热加载 server + 失效 token 缓存。

    双频道复用同一条 pubsub 连接（redis-py 支持多频道 subscribe），
    自愈逻辑只维护一个连接——避免第二条连接带来双倍断线面。
    """
    r = get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe("server:changed", "token:changed")
    async for msg in pubsub.listen():
        if msg.get("type") != "message":
            continue
        try:
            channel = msg.get("channel", "")
            if channel == "token:changed":
                from auth import invalidate_token_cache
                data = json.loads(msg["data"])
                invalidate_token_cache(data["token_hash"])
                continue
            parsed = parse_change_event(msg["data"])
            if not parsed:
                continue
            action, name = parsed
            info = await r.hgetall(f"servers:{name}")
            if action == "remove":
                await _unmount_one(gateway, name)
            elif action in ("add", "update", "enable", "disable", "stop") and info:
                await _sync_one(gateway, name, info)
        except Exception as e:
            logger.error("watch_changes_event_failed", error=str(e), service="gateway-proxy")
```

- [ ] **Step 5: admin tokens.py 补 publish**

create_token 末尾（hset 后）与 delete_token（删除后）各加：

```python
# 缓存失效通知：proxy 本地 token 缓存靠此即时失效（删除/变更不发布 =
# 吊销延迟 60s，安全漏洞）
try:
    await r.publish("token:changed", json.dumps({"token_hash": token_hash}))
except Exception as e:
    logger.warning("token_publish_failed", error=str(e), service="gateway-admin")
```

（delete_token 已有 `token_hash = await r.get(f"token_id:{token_id}")` 查询，直接用。）

- [ ] **Step 6: 测试发布 + 全量回归**

admin：`tests/test_tokens.py` 加断言 publish 调用（monkeypatch fake_redis.publish 计数，create/delete 各 1 次）。
Run: `cd gateway-admin && uv run pytest tests/ -q` + `cd gateway-proxy && uv run pytest tests/ -q`
Expected: 全绿

- [ ] **Step 7: Commit**

```bash
git add gateway-proxy/auth.py gateway-proxy/registry.py gateway-admin/api/tokens.py gateway-proxy/tests/test_auth.py gateway-admin/tests/test_tokens.py
git commit -m "feat: proxy token 本地 TTL 缓存 + token:changed 失效通道（防吊销延迟/403 风暴）"
```

---

### Task 4: proxy Client 复用 + 背压 + 总超时 + pubsub 自愈

**Files:**
- Modify: `gateway-proxy/registry.py`（_mount_one client_factory 复用 + unmount 显式关闭 + watch_changes 自愈）
- Modify: `gateway-proxy/permission_middleware.py`（semaphore + wait_for）
- Modify: `gateway-admin/api/servers.py`（ServerCreate/Update 加 call_timeout）
- Test: `gateway-proxy/tests/test_registry.py`、`gateway-proxy/tests/test_permission_middleware.py`、`gateway-admin/tests/test_servers.py`

**Interfaces:**
- Consumes: `create_proxy(url)`（FastMCP，现有）；`servers:{name}` hash 现有 url/description/status/tools 字段
- Produces: `_mounted_clients: dict[str, Client]`（registry 模块级，name → 缓存 Client）；`_BACKEND_SEMAPHORES: dict[str, asyncio.Semaphore]`；per-server `call_timeout` 读法 `_get_call_timeout(name) -> float`

- [ ] **Step 1: 写失败测试 — client_factory 复用（不新建 Client）**

```python
async def test_mount_reuses_client(gateway_stub):
    """_mount_one 两次（同 name）只创建 1 个底层 Client（连接池复用）。"""
    import registry
    from fastmcp.server.providers.proxy import ProxyClient
    created = []
    def _counting_factory(url):
        def factory():
            c = ProxyClient(url)
            created.append(c)
            return c
        return factory
    monkeypatch.setattr(registry, "_make_client_factory", _counting_factory)
    await registry._mount_one(gateway_stub, "test-srv", "http://backend:9050/mcp")
    # 触发一次工具调用模拟
    ...
    assert len(created) == 1
```

（实现细节：`_make_client_factory(url)` 返回闭包，闭包内 `_mounted_clients.get(url)` 缓存 Client，未命中才新建。测试用 counting factory 验证只建一次。）

- [ ] **Step 2: 跑测试验证失败**

Run: `cd gateway-proxy && uv run pytest tests/test_registry.py -q`
Expected: FAIL — `_make_client_factory` AttributeError

- [ ] **Step 3: registry.py _mount_one 改用复用 client_factory**

```python
# 模块级：name → 缓存 Client（连接池复用，解决每请求新建 Client+httpx2 连接池问题）
_mounted_clients: dict[str, Client] = {}
_mounted_urls: dict[str, str] = {}  # name → url（unmount 时定位缓存）


def _get_or_create_client(url: str) -> Client:
    """返回缓存 Client 或新建（create_proxy 直接接受 Client 实例）。

    create_proxy 默认 factory 每次 _get_client() 新建 Client + httpx2 连接池
    （_httpx_utils.py:95，已核实）——每请求一次 TCP+TLS。缓存 Client 实例后
    transport 连接池复用。共享 Client 无默认 Authorization 头（key 串用
    防护 R5，后端鉴权走自己的认证）。
    """
    from fastmcp.server.providers.proxy import ProxyClient
    client = _mounted_clients.get(url)
    if client is None:
        client = ProxyClient(url)
        _mounted_clients[url] = client
    return client


async def _mount_one(gateway, name: str, url: str) -> None:
    ...
    try:
        # create_proxy(target) 接受 Client 实例（fastmcp server.py:2509 文档
        # "A Client instance (connected or disconnected)"）——传缓存实例复用连接池
        proxy = create_proxy(_get_or_create_client(url))
        gateway.mount(proxy, namespace=name)
        _mounted_urls[name] = url
    except Exception as e:
        ...
```

（若 Client 实例作为 target 的 session 生命周期有回归（R11）——工具调用需要每次 fresh session——则回退方案：`create_proxy(client_factory=_make_client_factory(url))`，factory 内返回缓存 Client 的 `new()` 派生（proxy.py:1179 `ProxyClient.new()` 官方派生机制复用底层 transport）。两种路径都保证连接池复用；先试 target=Client 实例，测试验证失败再走 client_factory。）

- [ ] **Step 4: _unmount_one 显式关闭 client**

```python
async def _unmount_one(gateway, name: str) -> None:
    ...
    # 显式关闭缓存 Client 的 transport 连接池（原实现靠 GC，连接泄漏）
    url = _mounted_urls.pop(name, None)
    client = _mounted_clients.pop(url, None) if url else None
    if client is not None:
        try:
            await client.aclose()
        except Exception as e:
            logger.warning("client_close_failed", server=name, error=str(e), service="gateway-proxy")
```

（`_mounted_urls: dict[str, str]` = name → url，_mount_one 时记录；Client 的关闭方法名需查 FastMCP Client API——`aclose()` 或 `close()`，实现时用 `dir()` 确认，测试断言 close 被调用。）

- [ ] **Step 5: permission_middleware 加背压 + 总超时**

```python
# 模块级：per-backend semaphore（默认 100）+ 每请求总超时（默认 90s ≥ 后端最长 60s 长任务）
_BACKEND_SEMAPHORES: dict[str, asyncio.Semaphore] = {}
_BACKEND_SEMAPHORE_LIMIT = int(os.environ.get("BACKEND_SEMAPHORE_LIMIT", "100"))
_CALL_TIMEOUT_DEFAULT = 90.0


def _get_semaphore(server: str) -> asyncio.Semaphore:
    sem = _BACKEND_SEMAPHORES.get(server)
    if sem is None:
        sem = asyncio.Semaphore(_BACKEND_SEMAPHORE_LIMIT)
        _BACKEND_SEMAPHORES[server] = sem
    return sem
```

`on_call_tool` 的调用段改为：

```python
# ── Call the backend（背压 + 总超时）──────────────────
server, _, _ = resolve_target(tool_name)  # 或 split_prefix 解析
sem = _get_semaphore(server)
timeout = _get_call_timeout(server)  # 从 servers:{name} hash 读 call_timeout，缺省 90s
async with sem:
    try:
        result = await asyncio.wait_for(call_next(context), timeout=timeout)
    except asyncio.TimeoutError as exc:
        # 超时计入审计（upstream_timeout），与 httpx 超时同分类
        ...
        raise ToolError(f"Backend timeout after {timeout}s")
```

（`_get_call_timeout` 读 Redis 每请求？否 — 启动/挂载时缓存到 `_mounted_timeouts: dict[str, float]`，unmount 清除。避免每请求 Redis。）

- [ ] **Step 6: watch_changes 补 pubsub 自愈（与搜索 MCP 对齐）**

`watch_changes` 的 listen 循环加断线重建（参照 tavily `_resubscribe` 模式）：

```python
async for msg in pubsub.listen():
    ...
    # 断线自愈：redis-py pubsub 断连不自动重连，必须重建订阅（tavily 同款）
    # 注意：listen() 在断连时可能抛错——捕获后 aclose 旧 pubsub → 重建 → subscribe 双频道
```

（实现细节：`listen()` 是 async generator，断连时抛异常退出循环。用 while True 包一层，except 重建 pubsub 后继续。）

- [ ] **Step 7: admin servers.py 加 call_timeout 字段**

```python
class ServerCreate(BaseModel):
    name: str
    url: str
    description: str = ""
    call_timeout: float | None = None  # 总超时秒；None → proxy 默认 90s

class ServerUpdate(BaseModel):
    url: str
    description: str = ""
    call_timeout: float | None = None
```

create/update 的 hset mapping 加 `"call_timeout": req.call_timeout or ""`。

- [ ] **Step 8: 全量回归 + Commit**

Run: `cd gateway-proxy && uv run pytest tests/ -q` + `cd gateway-admin && uv run pytest tests/ -q`
Expected: 全绿

```bash
git add gateway-proxy/registry.py gateway-proxy/permission_middleware.py gateway-admin/api/servers.py gateway-proxy/tests/ gateway-admin/tests/test_servers.py
git commit -m "feat: proxy client 复用/背压/总超时 90s/pubsun 自愈 + admin call_timeout 字段"
```

---

### Task 5: 搜索 MCP — httpx client 复用（tavily 先做，brave/serpapi 同构复制）

**Files:**
- Modify: `tavily-mcp/tavily_client.py`（共享 client 薄封装）
- Modify: `tavily-mcp/tools/search.py`（`_default_factory` 用共享 client）
- Test: `tavily-mcp/tests/test_tavily_client.py`、`tavily-mcp/tests/test_tools.py`

**Interfaces:**
- Consumes: 现有 `TavilyClient(key, timeout, transport)` 构造（transport 注入测试用）
- Produces: `get_shared_client() -> httpx.AsyncClient`（模块级单例）；`TavilyClient` 公开方法签名不变

- [ ] **Step 1: 写失败测试 — 共享 client 单例 + key 请求级**

```python
def test_shared_client_singleton():
    """get_shared_client 多次调用返回同一实例（连接池复用）。"""
    from tavily_client import get_shared_client
    assert get_shared_client() is get_shared_client()


async def test_tavily_client_no_default_auth_header():
    """共享 client 无默认 Authorization 头（R5 key 串用防护）。"""
    from tavily_client import get_shared_client
    client = get_shared_client()
    assert "Authorization" not in client.headers  # 共享 client 恒无默认凭证
```

- [ ] **Step 2: 跑测试验证失败**

Run: `cd tavily-mcp && uv run pytest tests/test_tavily_client.py -q`
Expected: FAIL — `get_shared_client` ImportError

- [ ] **Step 3: tavily_client.py 加共享 client**

```python
"""Tavily REST API client — 共享 httpx client 薄封装。

改造前每调用新建 AsyncClient（TCP+TLS 握手每请求一次）；现在进程级单例
httpx.AsyncClient（连接池复用），key 走请求级 headers（共享 client 禁止
默认 Authorization 头——防 key 串用 R5）。per-request timeout 由 httpx
每请求传参支持。公开方法签名不变（search(params) 等），factory 签名
(key, timeout) 不变——只改内部实现。
"""
import time
import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from key_pool import ErrorKind

logger = structlog.get_logger()
tracer = trace.get_tracer("tavily_mcp.tavily_client")

API_BASE = "https://api.tavily.com"
RETRYABLE_IF_IDEMPOTENT = {"search", "extract", "map"}

# 进程级共享 client：连接池复用，禁止默认 Authorization 头（R5）
_shared_client: httpx.AsyncClient | None = None
_SHARED_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=50)


def get_shared_client() -> httpx.AsyncClient:
    """进程级单例 httpx.AsyncClient（连接池复用）。"""
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            timeout=30.0,  # 兜底超时；工具层 per-request 覆盖
            limits=_SHARED_LIMITS,
        )
    return _shared_client
```

`TavilyClient` 改造 — `__init__` 不再自建 client，存 key + 引用共享 client（transport 注入仅测试用）：

```python
class TavilyClient:
    """Thin async client. transport injectable for tests (httpx ASGI/mock)."""

    def __init__(self, key: str, timeout: float = 5.0, transport=None):
        self._key = key
        self._timeout = timeout
        self._http = get_shared_client() if transport is None else httpx.AsyncClient(
            timeout=timeout, transport=transport)
```

每请求调用改显式 key 头 + per-request timeout：

```python
    async def _post(self, endpoint: str, params: dict) -> dict:
        with tracer.start_as_current_span(f"tavily_client.{endpoint}") as span:
            span.set_attributes({"http.method": "POST", "http.url": f"{API_BASE}/{endpoint}"})
            start = time.monotonic()
            resp = await self._http.post(
                f"{API_BASE}/{endpoint}", json=params,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=self._timeout,
            )
            ...
```

（usage() 同改。`close()` 保留但只在测试注入 transport 时真正关闭；共享 client 不 close——进程级存活。测试断言：注入 transport 的 FakeClient 不受影响。）

- [ ] **Step 4: 全量回归**

Run: `cd tavily-mcp && uv run pytest tests/ -q`
Expected: 全绿（原 63 通过；FakeClient 注入测试不变，因公开签名不变）

- [ ] **Step 5: brave/serpapi 同构复制**

重复 Step 1-4 于 brave-mcp（`brave_client.py`/`tools/web.py`）与 serpapi-mcp（`serpapi_client.py`/`tools/search.py`）：
- 共享 client 单例 + 请求级 key 头 + per-request timeout
- 测试：`test_shared_client_singleton` + `test_no_default_auth_header` 各一份

Run: `cd brave-mcp && uv run pytest tests/ -q` + `cd serpapi-mcp && uv run pytest tests/ -q`
Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add tavily-mcp/tavily_client.py tavily-mcp/tools/search.py tavily-mcp/tests/ brave-mcp/ serpapi-mcp/
git commit -m "feat(search-mcp): httpx 共享 client 复用（请求级 key 头 + per-request timeout），三源同构"
```

---

### Task 6: 搜索 MCP — KeyPool 借用语义 + pipeline + 退避

**Files:**
- Modify: `tavily-mcp/key_pool.py`（借用 + 锁 + pipeline）
- Modify: `tavily-mcp/tools/search.py`（429 退避 + semaphore）
- Test: `tavily-mcp/tests/test_key_pool.py`（借用分散测试）、`tavily-mcp/tests/test_tools.py`
- 同构复制：brave-mcp、serpapi-mcp

**Interfaces:**
- Consumes: 现有 `next_key()/on_success()/on_error()/reload()`（key_pool.py）
- Produces: 上述方法签名不变（借用语义内部化）；`_in_flight: dict[str, int]`（key_id → in-flight 数）

- [ ] **Step 1: 写失败测试 — 借用分散（2 key × 2 并发不撞同一 key）**

```python
async def test_next_key_borrow_spreads(fake_redis):
    """2 个健康 key 时并发借 2 次，分散到不同 key（借用语义防 429 风暴）。"""
    pool = await make_pool(fake_redis, keys=[k1, k2])  # 2 key 同 remaining=100
    r1 = await pool.next_key()
    r2 = await pool.next_key()  # 第一次借用未归还时，第二次应选另一个
    assert r1["key_id"] != r2["key_id"]
```

- [ ] **Step 2: 跑测试验证失败**

Run: `cd tavily-mcp && uv run pytest tests/test_key_pool.py::test_next_key_borrow_spreads -q`
Expected: FAIL — 现状两 key 都选 remaining 最高的，返回同一 key

- [ ] **Step 3: key_pool.py 加借用 + 锁 + pipeline**

```python
# ── 并发借用语义 ──────────────────────────────────────
# 改造前 next_key 只挑选不标记——并发请求全选 remaining 最高的 key → 429
# 风暴。借用：选择时临时递减 in-flight 计数（后续请求自然分散到别的 key），
# 完成/失败后 on_success/on_error 归还。单实例内 asyncio.Lock 足够；
# 多实例需 Redis 原子借出（Lua），留作演进（spec 3.2 边界）。
_in_flight: dict[str, int] = {}
_pool_lock: asyncio.Lock = asyncio.Lock()


async def next_key(self) -> dict | None:
    async with _pool_lock:
        rec = self._pick_candidate()
        if rec is None:
            return None
        _in_flight[rec["key_id"]] = _in_flight.get(rec["key_id"], 0) + 1
        # 借用期间 remaining 临时扣减——下一次选择自然避开
        rec["_effective_remaining"] = (rec.get("remaining") or 0) - _in_flight[rec["key_id"]]
        return rec
```

（`_pick_candidate` 从原 next_key 逻辑抽出：健康/低配额分桶 + 排序，排序键改为 `_effective_remaining`。`on_success/on_error/reload` 均加 `async with _pool_lock`；**锁绝不包外呼 await**（工具层在 next_key 返回后才调 API）。`reload()` 内整表替换 `_records` 在锁内——防热更新与 in-flight 记账竞态（spec 3.2 已核实）。）

归还逻辑 — on_success/on_error 开头：

```python
async def on_success(self, key_id: str, remaining: int | None = None) -> None:
    async with _pool_lock:
        if _in_flight.get(key_id):
            _in_flight[key_id] -= 1
        rec = self._records.get(key_id)
        if rec is None:
            return
        ...
```

pipeline 化（spec 3.3）— on_success 的 hset+zadd+expire 三连：

```python
        pipe = self._redis.pipeline()
        pipe.hset(self._pool_key, key_id, json.dumps(rec, ensure_ascii=False))
        now = time.time()
        pipe.zadd(f"search:usage:{self.provider}:{key_id}", {str(now): now})
        pipe.expire(f"search:usage:{self.provider}:{key_id}", 60 * 24 * 32)
        await pipe.execute()  # 一次往返替代三次
```

（`_write` 保留单命令版本供 on_error/reload 用，或统一走 pipeline。`on_error` 也加锁。）

- [ ] **Step 4: 429 退避 + 并发上限（tools/search.py）**

`_call_with_pool` 加重试退避（幂等操作 429 重试时）与 per-endpoint semaphore：

```python
# 模块级：per-endpoint 并发上限（长任务小并发防打爆外部 API）
_semaphores: dict[str, asyncio.Semaphore] = {}
_TOOL_CONCURRENCY = {"tavily_search": 20, "tavily_extract": 20, "tavily_map": 20,
                     "tavily_crawl": 5, "tavily_research": 5}


def _get_semaphore(tool_name: str) -> asyncio.Semaphore:
    sem = _semaphores.get(tool_name)
    if sem is None:
        sem = asyncio.Semaphore(_TOOL_CONCURRENCY[tool_name])
        _semaphores[tool_name] = sem
    return sem
```

重试路径（原 `key_rec2 = await pool.next_key()` 后立即重试）改为：

```python
# 429/冷却：指数退避再重试（0.5s 起步），不立即重打冷却 key
if kind == ErrorKind.RATE_LIMIT and retryable:
    await asyncio.sleep(0.5)
    key_rec2 = await pool.next_key()
```

- [ ] **Step 5: 全量回归（tavily + brave + serpapi）**

Run: 三个目录 `uv run pytest tests/ -q`
Expected: 全绿（借用分散测试通过；原有 next_key/on_success/on_error 测试不受签名变化影响）

- [ ] **Step 6: Commit**

```bash
git add tavily-mcp/key_pool.py tavily-mcp/tools/search.py tavily-mcp/tests/ brave-mcp/ serpapi-mcp/
git commit -m "feat(search-mcp): KeyPool 借用语义+锁防429风暴/pipeline 合并往返/429 退避/并发上限"
```

---

### Task 7: 规范沉淀 — templates + 各 CLAUDE.md + knowledge-base 重构

**Files:**
- Modify: `templates/mcp-template/CLAUDE.md`（并发规范 C1-C6 + §5 代理示范修正 + 3 处知识库路径）
- Modify: `gateway-proxy/CLAUDE.md`（审计数据流改写）
- Modify: `gateway-admin/CLAUDE.md`（消费者 + 旧描述）
- Modify: `CLAUDE.md`（根：速查表 + 5 处旧描述）
- Move: `knowledge-base/search-mcp-key-pool-pattern.md` → `knowledge-base/patterns/`、`knowledge-base/mcp-account-level-permission-pattern.md` → `knowledge-base/patterns/`、`knowledge-base/mcp-production-deployment-pitfalls.md` → `knowledge-base/pitfalls/`
- Create: `knowledge-base/patterns/audit-async-stream-pattern.md`
- Modify: `knowledge-base/README.md`（场景触发表索引）
- Test: 无（文档任务；验证 = grep 链接有效性）

**Interfaces:** 无（纯文档）

- [ ] **Step 1: templates/mcp-template/CLAUDE.md — 新增并发规范章节**

在「Redis 通用坑」前插入：

```markdown
## 并发与性能规范（必须）

### C1. HTTP client 必须复用
- 禁止每调用新建 httpx.AsyncClient（连接池重建 + TCP+TLS 握手每请求一次，实测为最大浪费）
- 三种正确形态：
  - 单后端 API：进程级单例 client（zabbix-mcp 样板）
  - 多 key 池：共享 client + 请求级 `headers={"Authorization": f"Bearer {key}"}`（tavily-mcp 样板）
  - SDK：按资源账户缓存 SDK client，凭证变化自动重建（aliyun-dns-mcp 样板）
- **共享 client 禁止设默认 Authorization 头**（防 key 串用 R5）
- per-request timeout（httpx 支持）替代每次新建 client 传 timeout

### C2. 外呼必须有超时
- 每个外部 API 调用必须有 timeout（默认 5s；长任务单独放宽）
- 长任务用 semaphore 限制并发（默认 ≤5）

### C3. 幂等重试必须带退避
- 429/限流：指数退避（0.5s/1s）再重试；冷却 key 不立即重打
- 非幂等操作禁止自动重试

### C4. Redis 每请求往返必须合并
- 热点路径（成功记账等）用 pipeline 合并多次写为一次往返
- 禁止每请求 3+ 次独立 Redis 命令

### C5. 共享状态必须考虑并发
- key 池等有状态组件：单实例内 asyncio.Lock（锁绝不包外呼 await，否则串行化背压失效）+ 借用语义（选择时扣减 in-flight，用完归还）
- 多实例需 Redis 原子操作（Lua）——单实例锁只保护本进程

### C6. pubsub 监听必须自愈
- 断线重建订阅（aclose → pubsub() → subscribe），禁止"断了只能重启容器"
- 多频道订阅复用同一条 pubsub 连接（redis-py 支持），自愈只维护一个连接
```

- [ ] **Step 2: 模板 §5 代理示范修正**

原：

```python
client = httpx.AsyncClient(timeout=10, proxy=proxy)   # ← 每调用新建，违反 C1
```

改：

```python
# 共享 client 单例（C1）：连接池复用，proxy 在创建时配置一次
_http_client: httpx.AsyncClient | None = None

def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=10, proxy=os.environ.get("SEARCH_PROXY") or None)
    return _http_client
```

- [ ] **Step 3: 模板知识库节 + key 池节路径更新**

line 51/52/53 + line 211：
- `knowledge-base/search-mcp-key-pool-pattern.md` → `knowledge-base/patterns/search-mcp-key-pool-pattern.md`
- `knowledge-base/mcp-account-level-permission-pattern.md` → `knowledge-base/patterns/mcp-account-level-permission-pattern.md`
- `knowledge-base/mcp-production-deployment-pitfalls.md` → `knowledge-base/pitfalls/mcp-production-deployment-pitfalls.md`

- [ ] **Step 4: gateway-proxy/CLAUDE.md 审计改写**

line 11 改：

```markdown
- Audit：全量调用（成功+失败）XADD 至 `audit:calls` stream（MAXLEN 50000）；MySQL 落库在 gateway-admin 消费者（XREADGROUP 批量 INSERT）。proxy 不直连 MySQL（审计异步化，MySQL 移出请求路径）
```

新增小节（token 缓存 / 背压超时 / pubsub 自愈 / client 复用）：

```markdown
## 并发加固（2026-08 实施）
- Token 缓存：本地 TTL 60s + `token:changed` 通道失效；Redis 故障缓存降级放行（防 403 风暴）
- Client 复用：create_proxy 传复用 client_factory（_mounted_clients 缓存），unmount 显式关闭
- 背压/超时：per-backend semaphore（默认 100）+ 总超时 90s（per-server call_timeout 覆盖）
- pubsub 自愈：watch_changes 断线重建订阅（server:changed + token:changed 同连接）
```

- [ ] **Step 5: gateway-admin/CLAUDE.md 改写**

line 10 改：

```markdown
- 读 MySQL（calls 表：聚合统计 + 请求明细 + 失败面板/轨迹）；审计消费者 XREADGROUP `audit:calls` 批量落库（lifespan 后台 task），Redis 仅 servers/tokens
```

新增：

```markdown
## 审计消费者（2026-08 实施）
- lifespan 启动 `audit_consumer._run_consumer()`：XREADGROUP batch=100/block=1s → executemany INSERT calls → XACK
- 落库失败连续 3 次 → batch 移 `audit:calls:dead` 死信流（XACK 防无限重试）
- 查询只读 MySQL calls 表，禁读 Redis stream
```

- [ ] **Step 6: 根 CLAUDE.md 修订**

- 速查表补一行：`| **并发规范** | 复用 client/超时/退避/pipeline/借用/pubsub 自愈 | 详细见 templates/mcp-template/CLAUDE.md「并发与性能规范」|`
- 5 处旧描述（:42/:46/:50/:121 等）按 spec 6.4 逐行改
- 架构图 `audit:failures（失败流）` → `audit:calls（全量审计缓冲流）`

- [ ] **Step 7: knowledge-base 目录重构 + 新模式文档**

```bash
mkdir -p knowledge-base/patterns knowledge-base/pitfalls
git mv knowledge-base/search-mcp-key-pool-pattern.md knowledge-base/patterns/
git mv knowledge-base/mcp-account-level-permission-pattern.md knowledge-base/patterns/
git mv knowledge-base/mcp-production-deployment-pitfalls.md knowledge-base/pitfalls/
```

新增 `knowledge-base/patterns/audit-async-stream-pattern.md`：

```markdown
# 审计异步化模式（Redis Stream 缓冲 + 消费者批量落库）

## 适用场景
网关/中间层需要全量调用审计，但同步写存储拖慢请求路径（QPS 千级时 MySQL INSERT 成瓶颈）。

## 架构
proxy 只 XADD `audit:calls` stream（MAXLEN 50000，成功+失败全量）→ 消费者（独立进程/lifespan task）XREADGROUP batch=100/block=1s → executemany 批量 INSERT → XACK；连续失败 batch 移 `audit:calls:dead` 死信流。

## 关键决策（D1-D4）
- 审计可丢、请求优先：XADD 失败仅日志+指标，不重试（R4）
- 落库延迟 <1s（block 1s + batch 100），失败面板"实时"变"准实时"（R1）
- 消费者挂 → stream 堆积 + MAXLEN 截断丢最老；XREADGROUP pending 恢复续读（R2）
- time 格式锁死（禁止顺手加毫秒——下游按秒切桶）

## 部署顺序（审计断档防护）
先起消费者进程（admin），再切 proxy 写入——stream 缓冲，零断档。
```

- [ ] **Step 8: knowledge-base/README.md 索引升级**

```markdown
# 知识库

## 自研模式（patterns/）
| 文件 | 模式 | 适用场景（触发条件） |
|---|---|---|
| `patterns/search-mcp-key-pool-pattern.md` | 多 API key 池 | 新搜索类 MCP / 需要 key 轮换 |
| `patterns/mcp-account-level-permission-pattern.md` | 账户级权限 | token 需要比 server 更细粒度权限 |
| `patterns/audit-async-stream-pattern.md` | 审计异步化 | 网关/高并发写审计 |

## 踩坑记录（pitfalls/）
| 文件 | 教训 | 适用场景 |
|---|---|---|
| `pitfalls/mcp-production-deployment-pitfalls.md` | 受限网络部署 | 生产构建/部署 |

## 官方文档（FastMCP v4）
`fastmcp-v4/` — 索引见 `fastmcp-v4/README.md`。

**强制规则**：写代码前必须先读对应知识库文件（触发场景见根 CLAUDE.md「知识库」节）。
```

- [ ] **Step 9: 验证链接有效性**

```bash
grep -rn "knowledge-base/search-mcp-key-pool-pattern\|knowledge-base/mcp-account-level\|knowledge-base/mcp-production" /Users/sunweini/mcpstore/ --include="*.md" | grep -v .venv
# 期望：0 结果（全部指向新路径）
```

- [ ] **Step 10: Commit**

```bash
git add templates/mcp-template/CLAUDE.md gateway-proxy/CLAUDE.md gateway-admin/CLAUDE.md CLAUDE.md knowledge-base/
git commit -m "docs: 并发规范沉淀 — templates C1-C6/各 CLAUDE.md 审计改写/knowledge-base patterns+pitfalls 重构+审计异步化模式"
```

---

### Task 8: 压测脚本 + 部署验证

**Files:**
- Create: `gateway-proxy/tests/load_test.py`（可独立运行压测脚本，非 pytest）
- Create: `deploy/verify_audit_pipeline.sh`（生产验证脚本）

**Interfaces:** 无（工具脚本）

- [ ] **Step 1: 写压测脚本**

```python
"""并发压测：httpx 打 gateway /mcp，验证并发加固效果。

用法: uv run python tests/load_test.py --url http://localhost:8082/mcp --token <tok> --concurrency 100
断言: 请求无失败 / XADD 写入数 = 请求数 / 无 403（token 缓存降级验证需另测）
"""
import argparse
import asyncio
import httpx

async def worker(client: httpx.AsyncClient, url: str, headers: dict, n: int, results: list):
    for i in range(n):
        try:
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": i, "method": "tools/call",
                "params": {"name": "tavily-mcp_tavily_search", "arguments": {"query": "test", "max_results": 1}},
            }, headers=headers, timeout=10)
            results.append(resp.status_code)
        except Exception as e:
            results.append(f"err:{type(e).__name__}")

async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8082/mcp")
    p.add_argument("--token", required=True)
    p.add_argument("--concurrency", type=int, default=100)
    p.add_argument("--requests", type=int, default=500)
    args = p.parse_args()
    headers = {"Authorization": f"Bearer {args.token}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=200)) as client:
        per = args.requests // args.concurrency
        results = []
        await asyncio.gather(*[worker(client, args.url, headers, per, results) for _ in range(args.concurrency)])
    ok = sum(1 for r in results if r == 200)
    print(f"total={len(results)} ok={ok} fail={len(results)-ok} rate={ok/len(results)*100:.1f}%")
    assert ok == len(results), "压测有失败"

if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: 本地压测（mock 后端）**

说明：本地压测需一个后端 MCP 可被 gateway 挂载。可用现有 tavily-mcp（本地起，REDIS_URL 本地 redis）+ gateway 挂载 + 压测。或最小 mock（FastMCP 空 server 返回固定结果）。

Run（本地）:
```bash
# 起 mock 后端 + gateway + 本地 redis，注册 server + token
cd gateway-proxy && uv run python tests/load_test.py --url http://localhost:8082/mcp --token <token> --concurrency 100
uv run python tests/load_test.py --url ... --concurrency 500
uv run python tests/load_test.py --url ... --concurrency 1000
```
Expected: 三档全 200，无失败。对比改造前（可选：git stash 跑基线）记录 p50/p95 改善。

- [ ] **Step 3: 写生产验证脚本**

`deploy/verify_audit_pipeline.sh`：

```bash
#!/bin/bash
# 生产验证：审计流水线（proxy XADD → admin 消费者 → MySQL calls 表）
# Usage: bash deploy/verify_audit_pipeline.sh <ssh_host>
set -euo pipefail
HOST="${1:?usage: verify_audit_pipeline.sh <ssh_host>}"

echo "[1/4] 检查容器状态"
ssh "$HOST" "docker compose -f /opt/mcp-gateway-cfg/deploy/docker-compose.yml ps" | grep -E "gateway-(proxy|admin).*Up" || { echo "FAIL: 容器未全 Up"; exit 1; }

echo "[2/4] 检查 stream 有数据"
ssh "$HOST" "docker exec \$(docker ps --filter name=redis -q) redis-cli XLEN audit:calls" 

echo "[3/4] 检查 calls 表有数据（消费者落库）"
ssh "$HOST" "docker exec \$(docker ps --filter name=mysql -q) mysql -umcp -p\$MYSQL_PASSWORD mcp_audit -e 'SELECT COUNT(*) FROM calls'" 2>/dev/null || echo "WARN: 查表需密码，改用手动验证"

echo "[4/4] 失败面板轨迹验证（人工）"
echo "  登录 http://$HOST:8081 → 请求日志页有数据 / 失败面板有轨迹"
echo "  触发一次失败调用后刷新失败面板，确认 message/journey 展示"
```

（生产验证分两步执行：`bash deploy.sh` 部署后先 `bash deploy/verify_audit_pipeline.sh` 验流水线，再人工验 UI。）

- [ ] **Step 4: 部署顺序说明（写进 RELEASE 或部署文档）**

在 `deploy/deploy.sh` 头部注释加：

```bash
# 部署顺序（并发加固后必守）：审计消费者在 gateway-admin，proxy 切 stream 写后
# 若 admin 未起 → 审计断档。先 `docker compose up -d gateway-admin` 再
# `docker compose up -d gateway-proxy`（或接受秒级断档，审计可丢）。
```

- [ ] **Step 5: Commit**

```bash
git add gateway-proxy/tests/load_test.py deploy/verify_audit_pipeline.sh deploy/deploy.sh
git commit -m "test: 压测脚本 + 生产审计流水线验证脚本 + 部署顺序说明"
```

---

## Self-Review 记录

（计划写完后的自审在此记录——见下方自审清单逐项核对）
