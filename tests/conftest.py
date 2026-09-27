"""测试公共夹具。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from collections.abc import Callable

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_user_dirs(tmp_path, monkeypatch):
    """状态/音频缓存默认落 ~/.subtitle-cli，测试一律改到 tmp。

    autouse：所有测试自动隔离，绝不污染真实的用户目录
    （与 test_vault.py 的"注入 tmp、不碰 HOME"约定一致）。
    """
    monkeypatch.setenv("SUBTITLE_CLI_STATE_DIR", str(tmp_path / "_state"))
    monkeypatch.setenv("SUBTITLE_CLI_AUDIO_CACHE_DIR", str(tmp_path / "_audio-cache"))
    monkeypatch.setenv("SUBTITLE_CLI_LOG_DIR", str(tmp_path / "_logs"))


@pytest.fixture(autouse=True)
def _force_no_color(monkeypatch):
    """typer/click 的帮助文本在 CI 终端探测下会带 ANSI 转义，导致 help 断言
    随运行环境漂移；一律强制无色彩，输出与平台/终端无关。"""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    monkeypatch.setenv("TERM", "dumb")


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    """从 tests/fixtures/ 加载录制的接口响应 JSON。"""

    def _load(name: str) -> Any:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return _load
