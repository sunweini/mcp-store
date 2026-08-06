"""Logging config regression — HTTP client library logs must not leak credentials.

阿里云 SDK（alibabacloud-tea-openapi）底层用 requests（经 urllib3），
其 RPC 请求 URL query 含 AccessKeyId（requests ConnectionError 消息带
"GET https://alidns.cn-hangzhou.aliyuncs.com/?...AccessKeyId=<明文>"）；
aiohttp 也在依赖树（fastmcp 传递依赖）。任何库 logger 按默认级别输出
请求详情都会泄漏真实凭证（spec §8.1 敏感防线）。configure_logging 必须
把这些库 logger 整体提到 WARNING——这是安全回归点。

不模拟 httpx 的"假回归"（审查 I4）：改为断言真实 logger 级别 + 用真实
logger 名发一次 INFO 验证被静默。
"""
import io
import logging


def test_http_lib_loggers_raised_above_info():
    from logging_config import configure_logging
    import structlog

    configure_logging([
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ])
    # 覆盖 SDK 全部可能打印请求日志的库：httpx（曾用）+ requests/urllib3
    # （tea-openapi 实际底层）+ aiohttp（fastmcp 传递依赖）——任何一条
    # 按默认 INFO 输出都意味着 URL query 里的 AccessKeyId 会进日志
    for lib in ("httpx", "requests", "urllib3", "aiohttp"):
        assert logging.getLogger(lib).level == logging.WARNING


def test_credential_url_logs_suppressed_for_real_loggers():
    """真实库 logger 的 INFO 级请求日志（含 URL 明文凭证）必须被静默。"""
    from logging_config import configure_logging
    import structlog

    buf = io.StringIO()
    configure_logging([
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ])
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    url_msg = ("HTTPSConnectionPool(host='alidns.cn-hangzhou.aliyuncs.com', port=443): "
               "Max retries exceeded ...: GET https://alidns.cn-hangzhou.aliyuncs.com/?"
               "AccessKeyId=LTAI5t-demo-secret-value&Signature=abc&version=2015-01-09")
    for lib in ("httpx", "requests", "urllib3", "aiohttp"):
        logger = logging.getLogger(lib)
        logger.addHandler(handler)
        # INFO 请求日志（httpx 的 "HTTP Request: ..."、urllib3 的
        # "Starting new HTTPS connection (1): ..."、requests 的重试摘要等形态）
        logger.info("HTTP Request: GET %s", url_msg)
        logger.removeHandler(handler)

    output = buf.getvalue()
    # WARNING 门槛：INFO 请求日志不输出，明文凭证不进日志
    assert output == ""
    assert "LTAI5t-demo-secret-value" not in output
    assert "AccessKeyId" not in output
