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
    # 注意：BV 号输入会触发 view 接口查询（网络路径），此处用纯解析即可判定的非法输入
    result = runner.invoke(app, ["https://www.bilibili.com/list/546195", "--output", str(tmp_path)])
    assert result.exit_code == 2
    assert "无法从输入中识别合集" in result.output


def test_empty_input_exits_2(tmp_path: Path):
    result = runner.invoke(app, ["", "--output", str(tmp_path)])
    assert result.exit_code == 2


def test_cookie_without_sessdata_notifies_and_stops_before_network(tmp_path: Path):
    """Cookie 缺 SESSDATA：给出提示并停止（此处输入也非法，不触网）。"""
    result = runner.invoke(
        app,
        ["https://www.bilibili.com/list/546195", "--cookie", "buvid3=x; b_nut=1", "--output", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "没有 SESSDATA 字段" in result.output


def test_cookie_bare_value_is_normalized(tmp_path: Path):
    """只粘贴 SESSDATA 的值：自动补全并继续（此处输入非法，不触网即退出）。"""
    result = runner.invoke(
        app,
        ["https://www.bilibili.com/list/546195", "--cookie", "xx%2Fyy%2Fzz", "--output", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "自动按 SESSDATA 处理" in result.output
