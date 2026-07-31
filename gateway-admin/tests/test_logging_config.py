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
