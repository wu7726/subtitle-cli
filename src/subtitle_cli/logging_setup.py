"""轮转文件日志：CLI 与网页共用（成熟化轮新增）。

只加不改：控制台输出维持现状（CLI 用 typer.echo，网页日志行进 STATE），
文件日志是纯增量的**事后取证**渠道——长 ASR 任务失败、网页兜底 except
吞掉的 traceback，都能在这里查到。Cookie 属敏感凭据，任何日志调用都不得
记录它（与 PRD §7 的落盘纪律一致）。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "subtitle_cli"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3


def log_dir(path: Path | None = None) -> Path:
    """日志目录；SUBTITLE_CLI_LOG_DIR 可覆盖（测试/多实例用）。"""
    if path is not None:
        return path
    override = os.environ.get("SUBTITLE_CLI_LOG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".subtitle-cli" / "logs"


def setup_logging(root: Path | None = None) -> logging.Logger:
    """配置轮转文件日志，返回项目 logger；重复调用安全（只挂一次）。

    文件日志是纯增量的取证渠道：目录建不出来等任何故障都降级为不写文件，
    绝不影响提取/迁移主流程（否则 stderr 会被 logging 的报错刷满）。
    """
    logger = logging.getLogger(_LOGGER_NAME)
    if any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        return logger
    try:
        target = log_dir(root)
        target.mkdir(parents=True, exist_ok=True)
        # mkdir(exist_ok=True) 对"路径被同名文件占用"会静默通过（Python 3.13 实测），
        # 用探测文件验证真正可写，坏路径在这里暴露并降级
        probe = target / ".log-probe"
        probe.touch()
        probe.unlink()
        # ponytail: 多进程同时写一个日志文件，轮转瞬间可能交错；单人本地工具可接受，
        # 真出问题就给 CLI/网页各配一份文件名
        handler = RotatingFileHandler(
            target / "subtitle-cli.log",
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,  # 首次写入才建文件，不产生空文件
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except OSError as exc:
        print(f"文件日志不可用（不影响功能）：{exc}", file=sys.stderr)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
