"""Logging config regression — httpx request logs must not leak credentials.

阿里云 SDK（alibabacloud-tea-openapi）的 RPC 请求 URL query 含
AccessKeyId，httpx 默认在 INFO 级打印 "HTTP Request: GET https://
alidns.cn-hangzhou.aliyuncs.com/?...&AccessKeyId=<明文>"（spec §8.1
敏感防线）。configure_logging 必须把 httpx logger 提到 WARNING——
这是安全回归点。
"""
import io
import logging


def test_httpx_logger_raised_above_info():
    from logging_config import configure_logging
    import structlog

    configure_logging([
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ])
    # httpx 请求日志（含 URL query 的明文 AccessKeyId）必须被静默；
    # WARNING 以下不输出——断言级别而非日志内容（内容断言脆弱）
    assert logging.getLogger("httpx").level == logging.WARNING


def test_httpx_logger_suppresses_credential_url_logs():
    """模拟 httpx 的 INFO 级请求日志：凭证明文不能进入输出。"""
    from logging_config import configure_logging
    import structlog

    buf = io.StringIO()
    configure_logging([
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ])
    # 挂一个捕获 handler 到 httpx logger，模拟 SDK RPC 请求日志
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.addHandler(handler)
    httpx_logger.info(
        "HTTP Request: GET https://alidns.cn-hangzhou.aliyuncs.com/?"
        "AccessKeyId=LTAI5t-demo-secret-value&Signature=abc&version=2015-01-09")
    httpx_logger.removeHandler(handler)

    output = buf.getvalue()
    # WARNING 门槛：INFO 请求日志不输出，明文凭证不进日志
    assert output == ""
    assert "LTAI5t-demo-secret-value" not in output
    assert "AccessKeyId" not in output
