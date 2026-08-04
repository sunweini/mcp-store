# 全量调用明细审计设计（gateway-proxy + admin）

日期：2026-08-04
状态：待审阅
相关：`2026-07-30-mcp-gateway-design.md`、`audit.py`（现有失败审计）

## 背景

gateway-proxy 现有审计只记**失败请求**（`audit:failures` Redis Stream，MAXLEN 10000），成功调用仅 Prometheus 聚合计数（内存、重启清零）。监控面板无逐条调用明细，排障与用量审计缺数据。

目标：记录**所有 tools/call**（成功+失败）的元数据到新 `audit:calls` 流，admin 加「请求日志」页展示。失败审计流 `audit:failures` 保留不动（现有失败面板依赖）。

## 需求（已确认）

1. 全量调用明细：所有 tools/call（成功+失败）写 `audit:calls`
2. 新增 `audit:calls` 流，保留 `audit:failures`（失败条目双写，互不干扰）
3. 字段：元数据 only（不含请求参数/响应内容）
4. 聚合计数仍用 Prometheus（重启清零可接受，明细流不丢）

## 架构决策

- **实现位置：PermissionMiddleware.on_call_tool 内记录**（方案 A）。该 middleware 已计时、已提取 token/server/tool/op 全部信息，成功路径加 `record_call(status=ok)`，失败路径在现有 `record_failure` 之外补 `record_call(status=fail)`。零逻辑重复。
- 备选方案 B（独立审计 middleware）否：计时/token 提取重复。
- **不合并流**：`audit:calls` 与 `audit:failures` 分开（用户确认），失败面板逻辑不破坏。

## 实现

### 数据流

```
client -> proxy on_call_tool
            ├─ call_next(转发后端) 成功 -> record_call(status=ok)  -> audit:calls
            └─ 异常/权限拒绝 -> record_failure(audit:failures, 现有不变)
                              + record_call(status=fail, error_type) -> audit:calls
admin GET /api/calls <- xrevrange audit:calls <- 前端「请求日志」页
```

### audit.py 新增 record_call

```python
# audit:calls 流：全量调用明细（成功+失败），MAXLEN 50000（约数月数据）
_CALLS_STREAM = "audit:calls"
_CALLS_MAXLEN = 50000


async def record_call(
    journey: list[dict],
    meta: dict,
    status: str,                       # "ok" | "fail"
    error_type: str | None = None,     # 失败时填（upstream_timeout/permission_denied/...）
) -> None:
    """Append a call record (success or failure) to audit:calls.

    meta: {trace_id, server, tool, op, token_name, latency_ms, time}
    journey: [{stage, state, ms}, ...] - 同 record_failure
    """
    r = get_redis()
    try:
        await r.xadd(_CALLS_STREAM, {
            "trace": meta["trace_id"],
            "server": meta["server"],
            "tool": meta["tool"],
            "op": meta["op"],
            "token_name": meta.get("token_name", ""),
            "latency_ms": meta["latency_ms"],
            "status": status,
            "error_type": error_type or "",
            "time": meta["time"],
        }, maxlen=_CALLS_MAXLEN, approximate=True)
    except Exception as e:
        logger.error("audit_call_write_failed", error=str(e))
```

### permission_middleware.py on_call_tool 补 record_call

现有 on_call_tool 结构（计时 + call_next + 异常分类 + record_call_failure）。在两路径补 record_call：

- **成功路径**（call_next 正常返回）：`await record_call(journey, meta, status="ok")`
- **失败路径**（ToolError 或异常）：现有 `await record_call_failure(...)` 不变，再补 `await record_call(journey, meta, status="fail", error_type=...)`

journey/meta 复用现有 on_call_tool 已构造的字段（trace_id、server、tool、op、token_name、latency_ms、time 均已在作用域内）。

### gateway-admin/api/calls.py（新建）

```python
"""请求明细 API：读 audit:calls Redis Stream。"""
import json
from fastapi import APIRouter, Depends

from auth import require_admin
from redis_client import get_redis

router = APIRouter(prefix="/api/calls", tags=["calls"])


@router.get("")
async def list_calls(server: str | None = None, status: str | None = None,
                     limit: int = 50, offset: int = 0,
                     _: str = Depends(require_admin)):
    """列出调用明细，倒序（最新在前）。可按 server/status 过滤。"""
    r = get_redis()
    # xrevrange 倒序读，多读 offset+limit 条再切片（流无原生 offset）
    entries = await r.xrevrange("audit:calls", count=offset + limit)
    out = []
    for _stream_id, fields in entries:
        rec = {
            "trace": fields.get("trace", ""),
            "server": fields.get("server", ""),
            "tool": fields.get("tool", ""),
            "op": fields.get("op", ""),
            "token_name": fields.get("token_name", ""),
            "latency_ms": int(fields.get("latency_ms", 0)),
            "status": fields.get("status", ""),
            "error_type": fields.get("error_type", "") or None,
            "time": fields.get("time", ""),
        }
        if server and rec["server"] != server:
            continue
        if status and rec["status"] != status:
            continue
        out.append(rec)
    return {"count": len(out), "data": out}
```

注：流式 offset 用"多读再切片"实现（简单，量级 <50000 可接受）；若未来量大改用游标。

### app.py 注册

```python
from api import servers, tokens, dashboard, keys, calls
app.include_router(calls.router)
```

### 前端「请求日志」页

- 新导航项「请求日志」（Sidebar，API Keys 之后）
- 路由 `/calls`
- 表格列：时间 / Server / Tool / Token / 操作(r/w) / 耗时(ms) / 状态(✓ ok / ✗ fail)
- 过滤：Server 下拉、状态(全部/成功/失败)
- 分页：上一页/下一页（offset 翻页）
- api/index.js 加 `getCalls(params)`

## 错误处理

- audit:calls 写入失败：仅记日志（`audit_call_write_failed`），不影响请求本身（审计是旁路）
- 流读取异常：API 返回空列表 + 500 日志

## 测试

- audit.py: record_call 写入 audit:calls + MAXLEN 截断
- permission_middleware.py: 成功路径 record_call(status=ok)、失败路径 record_call(status=fail) + record_failure 双写
- api/calls.py: list_calls 过滤（server/status）、分页、空流
- 现有 on_call_tool 测试无回归（record_call 失败不阻断主流程）

## 部署影响

- gateway-proxy（audit.py + permission_middleware.py）+ gateway-admin（api/calls.py + app.py + 前端）重建
- 新增 Redis key audit:calls（MAXLEN 50000，内存占用可控）
- 无 schema/配置变更

## 非目标

- 不审计 tools/list/ping（只记 tools/call）
- 不记请求参数/响应内容（元数据 only）
- 聚合计数不持久化（仍 Prometheus，重启清零可接受）
- 不合并 audit:failures（保留独立）
