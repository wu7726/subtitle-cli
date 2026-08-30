"""手动集成测试：真实调用B站接口（技术方案 §9）。

运行前提（默认不运行，避免常规 pytest 触网）：
    SUBTITLE_CLI_INTEGRATION=1 pytest -m integration

- test_live_list_episodes_and_cid：无需登录。
- test_live_full_pipeline：需要 BILI_COOKIE（含 SESSDATA），字幕列表仅在登录态下返回。
- 合集可通过 SUBTITLE_CLI_TEST_SEASON 覆盖（默认为一个 58 集的实测合集）。
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from subtitle_cli.bilibili.client import BilibiliClient

pytestmark = pytest.mark.integration

SEASON_INPUT = os.environ.get("SUBTITLE_CLI_TEST_SEASON", "8016518")
INTEGRATION_ENABLED = os.environ.get("SUBTITLE_CLI_INTEGRATION") == "1"
HAS_COOKIE = bool(os.environ.get("BILI_COOKIE"))

needs_integration = pytest.mark.skipif(
    not INTEGRATION_ENABLED, reason="设置 SUBTITLE_CLI_INTEGRATION=1 才运行真实接口测试"
)
needs_cookie = pytest.mark.skipif(not HAS_COOKIE, reason="字幕列表需要登录态：设置 BILI_COOKIE")


@needs_integration
def test_live_list_episodes_and_cid():
    with BilibiliClient() as client:
        season_id = client.resolve_input(SEASON_INPUT)
        name, episodes = client.list_episodes(season_id)
        assert name, "合集名不应为空"
        assert episodes, "合集不应为空"
        assert episodes[0].index == 1 and episodes[-1].index == len(episodes)
        cid = client.fetch_cid(episodes[0].bvid)
        assert cid.isdigit()


@needs_integration
@needs_cookie
def test_live_fetch_subtitles_with_cookie():
    """登录态下至少应拿到一集字幕（CC 或 AI），并解析出非空行。"""
    from subtitle_cli.bilibili.models import Episode

    with BilibiliClient(cookie=os.environ["BILI_COOKIE"]) as client:
        _, episodes = client.list_episodes(client.resolve_input(SEASON_INPUT))
        track = client.fetch_subtitles(episodes[0])
        assert track is not None, "第一集无字幕（登录态下仍为空则接口可能变化）"
        assert track.lines, "字幕行不应为空"
        assert all(line.content.strip() for line in track.lines[:3])


@needs_integration
@needs_cookie
def test_live_full_pipeline(tmp_path):
    """端到端：真实合集 → Markdown 落盘，退出码 0 或 1（存在无字幕/失败分集时为 1）。"""
    from subtitle_cli.cli import app

    runner = CliRunner()
    result = runner.invoke(app, [SEASON_INPUT, "--output", str(tmp_path)])
    assert result.exit_code in (0, 1), result.output
    markdown_files = list(tmp_path.rglob("*.md"))
    assert markdown_files, f"应产出至少一个 Markdown 文件；输出：\n{result.output}"
