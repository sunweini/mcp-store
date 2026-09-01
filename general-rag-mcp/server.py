"""general-rag-mcp — General RAG 知识库检索 MCP Server

让任意 agent 通过 MCP 检索 general-rag 知识库（章管家接口文档等），
返回可置信、带来源标记的答案。

后端：general-rag REST API（生产 http://10.33.17.72:8001/api/v1，无鉴权，内网）。
协议：MCP 2026-07-28（stateless HTTP）；FastMCP 4.0.0b1。
Gateway-ready：annotations 读写分离 / tool 描述 / MCP ping 探活 / structlog + OTel。

隐私约束（指南 §3）：只允许内网访问，日志/返回体不完整转储文档内容。
"""
import os

import structlog
from fastmcp import FastMCP
from opentelemetry import trace

# NOTE: 端口 9055 是根 CLAUDE.md 端口表登记的最小未用端口（9050-9054 已占用）。
GENERAL_RAG_BASE_URL = os.environ.get(
    "GENERAL_RAG_BASE_URL", "http://10.33.17.72:8001/api/v1"
)
GENERAL_RAG_TIMEOUT = float(os.environ.get("GENERAL_RAG_TIMEOUT", "60"))
MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
MCP_PORT = int(os.environ.get("MCP_PORT", "9055"))
LOG_FORMAT = os.environ.get("LOG_FORMAT", "console")  # "console" or "json"


def _configure_logging() -> None:
    """Configure structlog with OTel trace context injection.

    OBS-CORR-001: 每条日志自动注入 trace_id + span_id。
    OBS-CORE-001: 所有日志结构化 key=value。
    """

    def add_trace_context(logger, method_name, event_dict):
        """从当前 OTel span 提取 trace_id/span_id 注入日志。"""
        span = trace.get_current_span()
        sc = span.get_span_context()
        if sc and sc.is_valid:
            event_dict["trace_id"] = format(sc.trace_id, "032x")
            event_dict["span_id"] = format(sc.span_id, "016x")
        return event_dict

    from logging_config import configure_logging
    configure_logging([
        structlog.contextvars.merge_contextvars,
        add_trace_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer() if LOG_FORMAT == "json"
        else structlog.dev.ConsoleRenderer(),
    ])


# 进程级 RagClient，模块加载时初始化（stateless 模式 lifespan 不可靠）。
# NOTE: 单进程单 loop 场景安全（生产 docker 单进程）；测试注入 transport 自建 client。
_client = None


def _get_client():
    """Return the process-level RagClient (module-level 懒加载单例)."""
    from rag_client import RagClient

    global _client
    if _client is None:
        _client = RagClient(
            base_url=GENERAL_RAG_BASE_URL, timeout=GENERAL_RAG_TIMEOUT
        )
    return _client


_configure_logging()

# OTel（metrics 降级不应杀服务）。
try:
    from telemetry import init_telemetry
    init_telemetry("general-rag-mcp")
except Exception as exc:
    structlog.get_logger().warning(
        "telemetry_init_failed", service="general-rag-mcp", error=str(exc)
    )

mcp = FastMCP(
    "general-rag-mcp",
    instructions=(
        "检索 general-rag 内部知识库（章管家接口文档等）并返回带来源标记的答案。"
        "知识库按 namespace 分区，检索必须先确认 namespace（默认 stamp-project）："
        "先用 knowledge_base_namespaces 查看可用命名空间，再 knowledge_base_search。"
        "knowledge_base_health 可探测组件状态。"
        "写入文档用 knowledge_base_ingest（⚠️ 写操作，需用户确认）。"
        "当 degraded=true 或 gap_warning 非空时，如实告知用户，不要臆测补充。"
    ),
)


# Register all tools — tools access the RagClient via _get_client closure.
from tools import register_tools

register_tools(mcp, _get_client)


if __name__ == "__main__":
    # NOTE: stateless_http=True 是接入 Gateway 的硬性要求。
    # MCP 标准 ping 由 FastMCP 原生提供，Gateway 据此探活，无需额外实现。
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        host=MCP_HOST,
        port=MCP_PORT,
    )
