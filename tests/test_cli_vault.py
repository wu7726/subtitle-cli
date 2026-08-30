"""CLI --vault 集成面单测（M7）：配置写回、vault 落盘、优先级（全程无网络）。"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from subtitle_cli.bilibili.models import Episode, SubtitleLine, SubtitleTrack
from subtitle_cli.cli import app

runner = CliRunner()


class FakeNetClient:
    """替代 BilibiliClient 的假客户端（CLI 构造后经 with 使用）。"""

    def __init__(self, cookie: str | None = None):
        self.cookie = cookie

    def __enter__(self) -> "FakeNetClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def resolve_input(self, raw: str) -> str:
        return "123"

    def list_episodes(self, season_id: str) -> tuple[str, list[Episode]]:
        return "测试合集", [
            Episode(bvid="BV1x", cid="c1", title="标题1", index=1),
            Episode(bvid="BV1x", cid="c2", title="标题2", index=2),
        ]

    def fetch_subtitles(self, episode: Episode) -> SubtitleTrack:
        return SubtitleTrack(
            lan="zh-CN",
            lines=[SubtitleLine(from_time=0, to_time=1, content=f"第{episode.index}集内容。")],
        )


def test_help_lists_vault_options():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--vault" in result.output
    assert "--vault-subdir" in result.output


def _patch(monkeypatch, tmp_path: Path) -> Path:
    """隔离配置文件路径并替换网络客户端。"""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr("subtitle_cli.vault.config_path", lambda: cfg_path)
    monkeypatch.setattr("subtitle_cli.cli.BilibiliClient", FakeNetClient)
    return cfg_path


def test_vault_end_to_end(tmp_path: Path, monkeypatch):
    vault_dir = tmp_path / "vault"
    cfg_path = _patch(monkeypatch, tmp_path)
    result = runner.invoke(app, ["123", "--vault", str(vault_dir)])
    assert result.exit_code == 0, result.output
    note = vault_dir / "B站字幕" / "测试合集" / "EP01 标题1.md"
    assert note.exists()
    assert note.read_text(encoding="utf-8").startswith("---\n")
    index = vault_dir / "B站字幕" / "测试合集" / "测试合集.md"
    assert index.exists()
    assert "episodes: 2" in index.read_text(encoding="utf-8")
    # 传入即记住（PRD §5.1）：配置文件被写回且不含敏感信息
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["vault"] == str(vault_dir)
    assert cfg["subdir"] == "B站字幕"
    assert "cookie" not in cfg and "SESSDATA" not in cfg_path.read_text(encoding="utf-8")


def test_vault_subdir_nesting(tmp_path: Path, monkeypatch):
    vault_dir = tmp_path / "vault"
    _patch(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["123", "--vault", str(vault_dir), "--vault-subdir", "学习/笔记"]
    )
    assert result.exit_code == 0, result.output
    assert (vault_dir / "学习" / "笔记" / "测试合集" / "EP01 标题1.md").exists()


def test_output_flag_overrides_configured_vault(tmp_path: Path, monkeypatch):
    vault_dir = tmp_path / "vault"
    cfg_path = _patch(monkeypatch, tmp_path)
    cfg_path.write_text(
        json.dumps({"vault": str(vault_dir), "subdir": "B站字幕"}), encoding="utf-8"
    )
    out = tmp_path / "plain-out"
    result = runner.invoke(app, ["123", "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert (out / "测试合集" / "EP01 标题1.md").exists()
    assert not (vault_dir / "B站字幕").exists()  # --output 显式给出 → 不进 vault
    plain = (out / "测试合集" / "EP01 标题1.md").read_text(encoding="utf-8")
    assert not plain.startswith("---\n")  # 普通模式仍是无属性头的旧格式
