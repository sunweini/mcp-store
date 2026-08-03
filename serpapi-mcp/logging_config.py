"""Structured logging setup: structlog + stdlib with optional file output.

OBS-CORE-001: structured key=value. LOG_FILE env enables a rotated file
handler alongside stdout so container logs persist on the host volume.
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
    # httpx 的请求日志默认 INFO 级且含完整 URL——serpapi 的 api_key 是
    # URL query 参数，真实 key 会随 "HTTP Request: GET https://serpapi.
    # com/search.json?...&api_key=..." 落入日志（冒烟实测确认）。这行是
    # 必守项，不是可选项：httpx 库日志整体提到 WARNING，行为不受影响。
    logging.getLogger("httpx").setLevel(logging.WARNING)
