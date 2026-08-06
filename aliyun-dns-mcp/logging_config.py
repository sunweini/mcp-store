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
    # httpx 的请求日志默认 INFO 级且含完整 URL——阿里云 SDK RPC 请求的
    # URL query 含 AccessKeyId，真实凭证会随 "HTTP Request: GET https://
    # alidns.cn-hangzhou.aliyuncs.com/?...AccessKeyId=..." 落入日志
    # （spec §8.1 敏感防线）。这行是必守项，不是可选项：httpx 库日志
    # 整体提到 WARNING，行为不受影响。
    logging.getLogger("httpx").setLevel(logging.WARNING)
