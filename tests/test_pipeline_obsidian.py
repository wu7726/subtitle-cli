"""obsidian 模式单测（M7：属性头 + 合集索引页，PRD F2/F3）。

复用 test_pipeline 的 FakeClient 模式；多P 用例以 season_id 以 BV 开头为约定。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from subtitle_cli.bilibili.client import BilibiliError
from subtitle_cli.bilibili.models import Episode, EpisodeStatus
from subtitle_cli.pipeline import (
    format_preview,
    preview_first_episode,
    run_collection,
    summarize,
)
from tests.test_pipeline import FakeClient, make_episodes, track_of

FIXED_DATE = date(2026, 1, 2)


class MultiPClient(FakeClient):
    """BV 开头输入原样返回（多P 视频约定）。"""

    def resolve_input(self, raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("BV"):
            return raw
        return super().resolve_input(raw)


def _read(tmp_path: Path, *parts: str) -> str:
    return tmp_path.joinpath(*parts).read_text(encoding="utf-8")


def test_obsidian_mode_wraps_frontmatter_and_builds_index(tmp_path: Path):
    logs: list[str] = []
    client = FakeClient(
        episodes=make_episodes(2),
        script={1: track_of("大家好。"), 2: track_of("第二集内容。")},
    )
    outcome = run_collection(
        "123", tmp_path, client, log=logs.append,
        note_mode="obsidian", fetched_at=FIXED_DATE,
    )
    text = _read(tmp_path, "测试合集", "EP01 标题1.md")
    assert text.startswith("---\n")
    assert 'source: "https://www.bilibili.com/video/BV01"' in text
    assert "created: 2026-01-02" in text
    assert 'author: ""' in text  # FakeClient 无 uploader_name 能力 → 留空
    assert "tags:\n  - B站字幕\n  - 测试合集" in text
    assert "fetched_by: subtitle-cli" in text
    # 正文一字不动
    assert "# 第1集 标题1\n\n大家好。\n" in text
    assert "已写入 vault：EP01 标题1.md" in logs
    assert "索引页已更新：测试合集.md（2 集）" in logs

    index = _read(tmp_path, "测试合集", "测试合集.md")
    assert "type: index" in index and "episodes: 2" in index
    assert "- [[EP01 标题1|第1集 标题1]]" in index
    assert "- [[EP02 标题2|第2集 标题2]]" in index
    assert outcome.index_path == str(tmp_path / "测试合集" / "测试合集.md")
    assert outcome.output_dir == str(tmp_path)
    assert "已写入 vault：" in summarize(outcome)


def test_obsidian_multi_p_omits_season_id_and_urls_carry_page(tmp_path: Path):
    eps = [
        Episode(bvid="BV1x", cid="c1", title="P1", index=1),
        Episode(bvid="BV1x", cid="c2", title="P2", index=2),
    ]
    client = MultiPClient(episodes=eps, script={1: track_of("一。"), 2: track_of("二。")})
    run_collection(
        "BV1x", tmp_path, client, log=lambda *_: None,
        note_mode="obsidian", fetched_at=FIXED_DATE,
    )
    text1 = _read(tmp_path, "测试合集", "EP01 P1.md")
    assert 'source: "https://www.bilibili.com/video/BV1x?p=1"' in text1
    text2 = _read(tmp_path, "测试合集", "EP02 P2.md")
    assert 'source: "https://www.bilibili.com/video/BV1x?p=2"' in text2
    index = _read(tmp_path, "测试合集", "测试合集.md")
    assert "season_id" not in index


def test_obsidian_index_includes_preexisting_files(tmp_path: Path):
    """跳过的旧分集也要进索引；旧文件本身一字不动。"""
    client = FakeClient(
        episodes=make_episodes(2),
        script={1: track_of("一。"), 2: track_of("二。")},
    )
    ep1 = tmp_path / "测试合集" / "EP01 标题1.md"
    ep1.parent.mkdir(parents=True)
    ep1.write_text("# 第1集 标题1\n\n旧正文\n", encoding="utf-8")
    outcome = run_collection(
        "123", tmp_path, client, log=lambda *_: None,
        note_mode="obsidian", fetched_at=FIXED_DATE,
    )
    statuses = {r.episode.index: r.status for r in outcome.results}
    assert statuses[1] == EpisodeStatus.SKIPPED
    assert ep1.read_text(encoding="utf-8").endswith("旧正文\n")  # 未被改写
    index = _read(tmp_path, "测试合集", "测试合集.md")
    assert "- [[EP01 标题1|第1集 标题1]]" in index
    assert "- [[EP02 标题2|第2集 标题2]]" in index


def test_obsidian_index_regenerated_each_run(tmp_path: Path):
    client = FakeClient(
        episodes=make_episodes(2),
        script={1: track_of("一。"), 2: track_of("二。")},
    )
    run_collection("123", tmp_path, client, log=lambda *_: None,
                   note_mode="obsidian", fetched_at=FIXED_DATE)
    index_path = tmp_path / "测试合集" / "测试合集.md"
    index_path.unlink()
    rerun = FakeClient(
        episodes=make_episodes(2),
        script={1: track_of("一。"), 2: track_of("二。")},
    )
    outcome = run_collection("123", tmp_path, rerun, log=lambda *_: None,
                             note_mode="obsidian", fetched_at=FIXED_DATE)
    assert outcome.index_path is not None and index_path.exists()
    assert "episodes: 2" in index_path.read_text(encoding="utf-8")
    assert all(r.status == EpisodeStatus.SKIPPED for r in outcome.results)


def test_obsidian_index_excludes_unsuccessful(tmp_path: Path):
    client = FakeClient(
        episodes=make_episodes(4),
        script={1: track_of("一。"), 2: None, 3: BilibiliError("接口错误"), 4: track_of("四。")},
    )
    run_collection("123", tmp_path, client, log=lambda *_: None,
                   note_mode="obsidian", fetched_at=FIXED_DATE)
    index = _read(tmp_path, "测试合集", "测试合集.md")
    assert "episodes: 2" in index
    assert "[[EP01" in index and "[[EP04" in index
    assert "[[EP02" not in index and "[[EP03" not in index


def test_obsidian_all_failed_writes_no_index(tmp_path: Path):
    client = FakeClient(episodes=make_episodes(1), script={1: BilibiliError("x")})
    outcome = run_collection("123", tmp_path, client, log=lambda *_: None,
                             note_mode="obsidian", fetched_at=FIXED_DATE)
    assert outcome.index_path is None
    assert not (tmp_path / "测试合集.md").exists()
    assert not (tmp_path / "测试合集").exists()  # 一集未落盘则不建空目录


def test_preview_obsidian_has_frontmatter_and_completeness_line():
    client = FakeClient(episodes=make_episodes(1), script={1: track_of("大家好。")})
    result = preview_first_episode("123", client, log=lambda *_: None, note_mode="obsidian")
    assert result.markdown.startswith("---\n")
    assert result.meta is not None
    assert "属性头字段：完整" in format_preview(result)


def test_preview_plain_has_no_frontmatter():
    client = FakeClient(episodes=make_episodes(1), script={1: track_of("大家好。")})
    result = preview_first_episode("123", client, log=lambda *_: None)
    assert not result.markdown.startswith("---\n")
    assert "属性头字段" not in format_preview(result)
