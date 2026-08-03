"""Logging config regression — httpx request logs must not leak api_key.

serpapi 的 api_key 是 URL query 参数。httpx 默认在 INFO 级打印
"HTTP Request: GET https://serpapi.com/search.json?...&api_key=<明文>"，
冒烟实测确认（tavily/brave 的 key 在 header/body，不受此问题影响）。
configure_logging 必须把 httpx logger 提到 WARNING——这是安全回归点。
"""
import logging


def test_httpx_logger_raised_above_info():
    from logging_config import configure_logging
    import structlog

    configure_logging([
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ])
    # httpx 请求日志（含 URL query 的明文 api_key）必须被静默；
    # WARNING 以下不输出——断言级别而非日志内容（内容断言脆弱）
    assert logging.getLogger("httpx").level == logging.WARNING
