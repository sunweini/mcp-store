"""Audit (success + failure): proxy 只 XADD audit:calls stream，MySQL 落库在 admin 消费者。

改造前 proxy 同步写 MySQL calls 表 + Redis audit:failures 双写；现在 MySQL
完全移出请求路径（D1/D3）——单流 audit:calls 承载成功+失败全量，消费者
（gateway-admin）XREADGROUP 批量落库。XADD 失败仅日志+指标（D4 审计可丢）。

(ADR-0001) 审计构造归本模块：build_audit_meta / build_journey / classify_error /
ERROR_TYPES 集中于此。server/tool/op 由调用方（on_call_tool 的 AuthResult）注入，
不再内部解析——授权与审计共享一次解析（授权是 mode 唯一解析点）。
"""
import time
import structlog
from redis_client import get_redis

logger = structlog.get_logger()

# NOTE: bounded enum consumed by the admin frontend's error-type chips.
# unknown_mode：mode 未知（fail-closed，工具未在 TOOL_REGISTRY 注册）。
ERROR_TYPES = frozenset({
    "upstream_timeout",
    "permission_denied",
    "invalid_token",
    "invalid_target",
    "unknown_mode",
    "upstream_error",
    "connection_error",
})

_STREAM = "audit:calls"
# MAXLEN trims the stream so it cannot grow unbounded (R9: 50000 条 = 千级 QPS 下 50s 缓冲)
_STREAM_MAXLEN = 50000


def classify_error(exc: Exception) -> str:
    """Map an exception to an audit error_type enum value.

    异常→审计错误类型的映射，属审计概念（生产的就是 ERROR_TYPES 枚举成员）。
    """
    import httpx
    if isinstance(exc, httpx.TimeoutException):
        return "upstream_timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connection_error"
    return "upstream_error"


def build_journey(fail_stage: str, server: str, latency_ms: int) -> list[dict]:
    """Build the request journey: stages before fail_stage are ok, the fail
    stage carries the total latency, and stages after it were never reached
    (skip).

    NOTE: 独立成函数是因为 on_call_tool 的三条路径（拒绝/异常/成功）共用同一套
    stage 推演逻辑构建轨迹，保证各路径写入 audit:calls 的 journey 结构一致。
    """
    stages = ["client", "gateway", "auth", "route", server or "backend"]
    journey = []
    for i, st in enumerate(stages):
        if st == fail_stage:
            journey.append({"stage": st, "state": "fail", "ms": latency_ms})
            # subsequent stages were not reached
            for after in stages[i + 1:]:
                journey.append({"stage": after, "state": "skip", "ms": 0})
            break
        journey.append({"stage": st, "state": "ok", "ms": 0})
    return journey


def build_audit_meta(
    token_info: dict | None,
    server: str,
    tool: str,
    op: str,
    latency_ms: int,
    trace_id: str,
) -> dict:
    """Build the audit meta dict (time/server/tool/op/token_name/latency_ms/trace_id).

    server/tool/op 由调用方（on_call_tool 的 AuthResult）注入——授权已解析出
    目标，审计不再重复解析。server 未注册（禁用后 registry 卸载）时 AuthResult
    仍能给出 server/tool（authorize 用 split_prefix 纯切分），故不管 registry
    状态，审计字段都不会落空。

    token_name 展示用的是哪个凭证（无 token 时 "(anonymous)"）；trace_id 把
    审计记录与 OTel span 关联起来。
    """
    token_name = token_info.get("name", "(anonymous)") if token_info else "(anonymous)"
    return {
        "trace_id": trace_id,
        "server": server,
        "tool": tool,
        "op": op,
        "token_name": token_name,
        "latency_ms": latency_ms,
        # time 格式锁死 %Y-%m-%d %H:%M:%S.000（固定 .000）：admin 消费者按此解析
        "time": time.strftime("%Y-%m-%d %H:%M:%S.000", time.gmtime()),
    }


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
