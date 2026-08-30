"""CLI 冒烟测试：--help 与非法输入退出码（技术方案 §9），全程无网络。"""

from pathlib import Path

from typer.testing import CliRunner

from subtitle_cli.cli import app

runner = CliRunner()


def test_help_exits_zero():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "用法" in result.output or "Usage" in result.output
    assert "--output" in result.output
    assert "--cookie" in result.output


def test_invalid_input_exits_2(tmp_path: Path):
    result = runner.invoke(app, ["BV1xx411c7mD", "--output", str(tmp_path)])
    assert result.exit_code == 2
    assert "仅支持" in result.output


def test_empty_input_exits_2(tmp_path: Path):
    result = runner.invoke(app, ["", "--output", str(tmp_path)])
    assert result.exit_code == 2
