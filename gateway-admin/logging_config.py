"""Structured logging setup: structlog + stdlib with optional file output.

OBS-CORE-001: structured key=value. LOG_FILE env enables a rotated file
handler alongside stdout so container logs persist on the host volume.

OBS: httpx 默认 INFO 级打印完整请求 URL——admin 探活 serpapi key 时
query 带 api_key 明文（_probe_key），必须把 httpx logger 提到 WARNING
（与三个搜索 MCP 的 logging_config 同防线；端到端实测抓到泄漏，见
task-8-report）。zabbix/tavily/brave 的 key 在 header/body 不受影响，
统一提级是为 serpapi 探活兜底。
"""
import logging
import os
from logging.handlers import RotatingFileHandler

import structlog


def configure_logging(processors: list) -> None:
    """Configure structlog to emit structured JSON to stdout (+ optional file).

    processors: service-specific chain (e.g. with merge_contextvars /
        add_trace_context) appended before the shared renderers.
    LOG_FILE env: if set, also write to this path, rotated 10MB x 5.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = os.environ.get("LOG_FILE")
    if log_file:
        handlers.append(
            RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        )
    # force=True: replace any prior handlers (tests / re-init safe)
    logging.basicConfig(
        level=logging.INFO, handlers=handlers, format="%(message)s", force=True
    )
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    # httpx 请求日志（含 URL query 的明文 api_key）必须被静默；
    # WARNING 以下不输出——探活 serpapi 时 api_key 在 URL 里（keys.py）
    logging.getLogger("httpx").setLevel(logging.WARNING)
