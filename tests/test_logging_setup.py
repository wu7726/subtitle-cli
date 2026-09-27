"""轮转文件日志单测：写入、幂等、目录覆盖。全程离线。

只断言自己拥有的 RotatingFileHandler：pytest 的日志插件（caplog）会往
全局 logger 上临时挂 LogCaptureHandler，不属于本模块的管理范围。
"""

import logging

import pytest
from logging.handlers import RotatingFileHandler

from subtitle_cli.logging_setup import log_dir, setup_logging


def _file_handlers(logger: logging.Logger) -> list[RotatingFileHandler]:
    return [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]


@pytest.fixture(autouse=True)
def _fresh_logger():
    """setup_logging 配的是全局 logger：清掉其他测试留下的文件 handler。"""
    logger = logging.getLogger("subtitle_cli")
    for h in _file_handlers(logger):
        logger.removeHandler(h)
    yield
    for h in _file_handlers(logger):
        logger.removeHandler(h)


def test_setup_logging_writes_file_and_is_idempotent(tmp_path):
    logger = setup_logging(tmp_path)
    assert logger is setup_logging(tmp_path)  # 重复调用不叠加 handler
    assert len(_file_handlers(logger)) == 1

    logger.info("提取开始：platform=%s", "bilibili")
    for h in _file_handlers(logger):
        h.flush()
    content = (tmp_path / "subtitle-cli.log").read_text(encoding="utf-8")
    assert "提取开始：platform=bilibili" in content


def test_log_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBTITLE_CLI_LOG_DIR", str(tmp_path / "elsewhere"))
    assert log_dir() == tmp_path / "elsewhere"
    assert log_dir(tmp_path) == tmp_path  # 显式参数优先


def test_setup_logging_degrades_when_path_is_file(tmp_path, capsys):
    """目标路径被同名文件占用：降级为不挂 handler、不抛，主流程不受影响。"""
    blocked = tmp_path / "blocked"
    blocked.write_text("占位文件", encoding="utf-8")
    logger = setup_logging(blocked)
    assert not _file_handlers(logger)
    assert "文件日志不可用" in capsys.readouterr().err
