"""Tests for logging_config: LOG_FILE enables a rotated file handler."""
import logging


def test_log_file_env_writes_to_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "svc.log"))
    import structlog
    from logging_config import configure_logging
    configure_logging([
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ])
    log = structlog.get_logger()
    log.info("test_event", key="value")
    for h in logging.getLogger().handlers:
        h.flush()
    content = (tmp_path / "svc.log").read_text()
    assert "test_event" in content
    assert "value" in content


def test_no_log_file_env_only_stdout(tmp_path, monkeypatch):
    """未设 LOG_FILE -> 没有 FileHandler,不抛错。"""
    monkeypatch.delenv("LOG_FILE", raising=False)
    import structlog
    from logging_config import configure_logging
    configure_logging([structlog.processors.JSONRenderer()])
    log = structlog.get_logger()
    log.info("ok_event")  # 不应抛异常
    assert not (tmp_path / "svc.log").exists()


def test_httpx_logger_raised_above_info():
    """httpx 请求日志必须被静默（探活 serpapi 时 api_key 在 URL query）。

    端到端实测抓到：admin 探活 serpapi key（GET /search.json?api_key=...）
    时 httpx 默认 INFO 级打印完整 URL，明文 key 落 admin 日志。提级到
    WARNING 后静默——断言级别而非日志内容（内容断言脆弱），与三个
    搜索 MCP 的 test_logging.py 同型防线。
    """
    import structlog
    from logging_config import configure_logging
    configure_logging([
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ])
    assert logging.getLogger("httpx").level == logging.WARNING
