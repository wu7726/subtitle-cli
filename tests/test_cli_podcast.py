"""CLI 播客链路冒烟：vault 分目录落盘、Cookie 忽略、--vault-subdir 覆盖。

create_client 打桩为 FakePodcastClient，全程无网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import subtitle_cli.cli as cli_mod
from subtitle_cli.cli import app
from tests.test_pipeline_podcast import SCRIPT, FakePodcastClient, make_episodes

runner = CliRunner()


@pytest.fixture
def fake_podcast(monkeypatch):
    created = []

    def _factory(raw: str, cookie: str | None = None):
        client = FakePodcastClient(make_episodes(), SCRIPT)
        created.append((raw, cookie, client))
        return client

    monkeypatch.setattr(cli_mod, "create_client", _factory)
    return created


def test_cli_podcast_vault_default_subdir(tmp_path: Path, monkeypatch, fake_podcast):
    monkeypatch.setenv("SUBTITLE_CLI_CONFIG", str(tmp_path / "config.json"))
    vault = tmp_path / "MyVault"
    vault.mkdir()

    result = runner.invoke(app, ["https://example.com/feed.xml", "--vault", str(vault)])

    assert result.exit_code == 0, result.output
    pod_dir = vault / "播客字幕" / "示例播客·离线调试"
    assert (pod_dir / "EP01 最早的一集.md").is_file()
    assert (pod_dir / "EP02 中间的一集.md").is_file()
    assert not (pod_dir / "EP03 最新的话题.md").exists()  # 无文稿
    assert "成功 2" in result.output
    # 配置已记住 vault；默认 podcast_subdir 不被改动
    import json

    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["podcast_subdir"] == "播客字幕"
    # Cookie 对播客输入被忽略（fake 收到 None 之外的透传值也要能跑通）
    assert fake_podcast[0][0] == "https://example.com/feed.xml"


def test_cli_podcast_explicit_subdir_overrides(tmp_path: Path, monkeypatch, fake_podcast):
    monkeypatch.setenv("SUBTITLE_CLI_CONFIG", str(tmp_path / "config.json"))
    vault = tmp_path / "MyVault"
    vault.mkdir()

    result = runner.invoke(
        app,
        ["https://example.com/feed.xml", "--vault", str(vault), "--vault-subdir", "学习/播客"],
    )

    assert result.exit_code == 0, result.output
    assert (vault / "学习" / "播客" / "示例播客·离线调试" / "EP01 最早的一集.md").is_file()


def test_cli_podcast_ignores_invalid_cookie(tmp_path: Path, monkeypatch, fake_podcast):
    """Cookie 缺 SESSDATA 对播客输入不拦截（B站 Cookie 校验只作用于B站）。"""
    monkeypatch.setenv("SUBTITLE_CLI_CONFIG", str(tmp_path / "config.json"))

    result = runner.invoke(
        app,
        [
            "https://example.com/feed.xml",
            "--output",
            str(tmp_path / "out"),
            "--cookie",
            "buvid3=x; b_nut=1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "示例播客·离线调试" / "EP01 最早的一集.md").is_file()


def test_cli_garbage_input_still_bilibili_error(tmp_path: Path, monkeypatch):
    """非 URL 输入回落B站路径，报旧版错误（退出码 2）。"""
    monkeypatch.setenv("SUBTITLE_CLI_CONFIG", str(tmp_path / "config.json"))

    result = runner.invoke(app, ["随便一段话", "--output", str(tmp_path / "out")])

    assert result.exit_code == 2
    assert "无法从输入中识别合集" in result.output


def test_cli_asr_missing_dependency_exits_2(tmp_path: Path, monkeypatch, fake_podcast):
    """--asr 且依赖未安装：快速失败并给出安装指引（退出码 2）。"""
    import sys

    monkeypatch.setenv("SUBTITLE_CLI_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    result = runner.invoke(
        app, ["https://example.com/feed.xml", "--output", str(tmp_path / "out"), "--asr"]
    )

    assert result.exit_code == 2
    assert "pip install" in result.output


def test_cli_asr_without_download_audio_warns_and_continues(
    tmp_path: Path, monkeypatch, fake_podcast
):
    """客户端不支持音频下载：告警一次，按普通提取继续（退出码 0）。"""
    from subtitle_cli import asr as asr_mod

    monkeypatch.setenv("SUBTITLE_CLI_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(asr_mod, "ensure_dependency", lambda: None)

    result = runner.invoke(
        app,
        ["https://example.com/feed.xml", "--output", str(tmp_path / "out"), "--asr"],
    )

    assert result.exit_code == 0, result.output
    assert "不支持音频下载" in result.output
    assert "成功 2" in result.output
