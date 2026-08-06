"""工具注册模块：模块级函数 + 显式具名包装（FastMCP v4 拒绝 *args 包装）。

工具函数定义在 tools/*.py 模块级（可独立测试），register() 只做薄包装：
注入真实 ctx（checker/clients），复制 docstring 作为 tool 描述。
"""
import functools
import time

import structlog
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from dataclasses import dataclass

from aliyun_client import AlidnsError

# CRITICAL: 运行时模块访问（`import telemetry` 而非 `from telemetry import X`）。
# 若用 from-import，模块加载瞬间（server.py 顶部 import tools 时，telemetry 尚
# 未 init）会把指标绑定为 None，init_telemetry() 随后只更新 telemetry 模块自身
# 的全局——_metrics_wrapper 的 guard 将永远跳过，aliyndns_requests_total 等
# 4 个指标静默失效（审查实测确认）。运行时取值免疫任何 import 顺序。
import telemetry

logger = structlog.get_logger()


@dataclass
class ToolContext:
    """工具依赖上下文：checker（账户级鉴权）+ clients（Alidns 客户端工厂）+ store（账户存储）。

    store 用于 I3 闭环：凭证失效时工具层直接禁用账户。测试可只传
    checker/clients（store 默认 None，_map_aliyun_error 据此跳过联动）。
    """
    checker: object
    clients: object
    store: object | None = None


async def map_aliyun_error(e: AlidnsError, account_id: str, ctx: ToolContext) -> dict:
    """AlidnsError → 工具返回结构；凭证失效时联动禁用账户（I3，spec §7.1）。

    为什么在工具层而非 _call：ClientFactory.get 拿到 client 后工具层真正
    调 API 才知道凭证失效——error_type 是 _call 分类出的最终结论，这里
    是唯一能拿到 account_id 上下文（客户端缓存键）的拦截点。
    disable_account 幂等 + PUBLISH 热更新，失败不阻断错误返回（fail-closed）。
    """
    if e.error_type == "invalid_credential" and ctx.store is not None:
        try:
            await ctx.store.disable_account(account_id)
        except Exception as disable_err:
            # 禁用失败不掩盖原始错误；告警日志给出原因（凭证安全：只记
            # account_id，不记凭证/异常内容可能含的 URL）
            logger.error("account_disable_failed", service="aliyun-dns-mcp",
                         account_id=account_id, reason=type(disable_err).__name__)
    return {"status": "error", "error_type": e.error_type,
            "message": e.message, "request_id": e.request_id}


def _metrics_wrapper(tool_name: str):
    """记录 tool 级 Prometheus 指标（zabbix 模式）。"""
    # 运行时取值：与 `import telemetry` 配套（见文件头 CRITICAL 注释），
    # 每调用读取当前 telemetry.REQUESTS_TOTAL 等，init_telemetry 后即生效。
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            requests_total = telemetry.REQUESTS_TOTAL
            if requests_total:
                requests_total.add(1, attributes={"tool_name": tool_name})
            in_flight = telemetry.IN_FLIGHT_REQUESTS
            if in_flight:
                in_flight.add(1)
            start = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                errors_total = telemetry.ERRORS_TOTAL
                if isinstance(result, dict) and result.get("status") == "error" and errors_total:
                    errors_total.add(1, attributes={"tool_name": tool_name, "error_type": "tool_error"})
                return result
            except Exception as e:
                errors_total = telemetry.ERRORS_TOTAL
                if errors_total:
                    errors_total.add(1, attributes={"tool_name": tool_name, "error_type": type(e).__name__})
                raise
            finally:
                duration = time.monotonic() - start
                request_duration = telemetry.REQUEST_DURATION
                if request_duration:
                    request_duration.record(duration, attributes={"tool_name": tool_name})
                if in_flight:
                    in_flight.add(-1)
        return wrapper
    return decorator


def register_tools(mcp: FastMCP, get_ctx, metrics=None) -> None:
    """注册全部工具。get_ctx: callable 返回 ToolContext（server.py 注入）。"""
    from tools import accounts, domains, records
    accounts.register(mcp, get_ctx, metrics=metrics or _metrics_wrapper)
    domains.register(mcp, get_ctx, metrics=metrics or _metrics_wrapper)
    records.register(mcp, get_ctx, metrics=metrics or _metrics_wrapper)
