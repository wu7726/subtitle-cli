"""播客跑通 pipeline 的集成单测：属性头、索引页、平台标签与失败分类。

FakePodcastClient 替代网络，验证 PodcastClient 与管线的协议契合。
"""

from __future__ import annotations

from pathlib import Path


from subtitle_cli import notes
from subtitle_cli.bilibili.models import Episode, EpisodeStatus, SubtitleLine, SubtitleTrack
from subtitle_cli.errors import PlatformError
from subtitle_cli.pipeline import format_preview, preview_first_episode, run_collection


class FakePodcastClient:
    platform = "podcast"
    base_tag = "播客字幕"

    def __init__(self, episodes: list[Episode], script: dict):
        self.episodes = episodes
        self.script = script

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def resolve_input(self, raw: str) -> str:
        return "https://example.com/feed.xml"

    def list_episodes(self, season_id: str) -> tuple[str, list[Episode]]:
        return "示例播客·离线调试", self.episodes

    def fetch_subtitles(self, episode: Episode) -> SubtitleTrack | None:
        outcome = self.script[episode.index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def uploader_name(self, bvid: str) -> str:
        return "主播小张"


def track_of(text: str, start: float = 0.0) -> SubtitleTrack:
    return SubtitleTrack(
        lan="zh", lines=[SubtitleLine(from_time=start, to_time=start + 2, content=text)]
    )


def make_episodes() -> list[Episode]:
    # 标题不带 EP 前缀：episode_filename 会自动加 EP{index:02d} 前缀
    return [
        Episode(bvid="ep1", title="最早的一集", index=1,
                source_url="https://example.com/episodes/1"),
        Episode(bvid="ep2", title="中间的一集", index=2,
                source_url="https://example.com/episodes/2", transcript_url="https://x/ep2.vtt"),
        Episode(bvid="ep3", title="最新的话题", index=3,
                source_url="https://example.com/episodes/3"),
    ]


SCRIPT = {
    1: track_of("第一集的播客内容，这一句比较长，用来凑满段落闭合条件。"),
    2: track_of("第二集也有文稿。"),
    3: None,  # 无文稿
}


def test_podcast_obsidian_run_writes_tagged_notes(tmp_path: Path):
    client = FakePodcastClient(make_episodes(), SCRIPT)

    outcome = run_collection("https://example.com/feed.xml", tmp_path, client, note_mode="obsidian")

    assert outcome.collection_name == "示例播客·离线调试"
    statuses = {r.episode.index: r.status for r in outcome.results}
    assert statuses[1] == EpisodeStatus.SUCCESS
    assert statuses[3] == EpisodeStatus.NO_SUBTITLE

    note = (tmp_path / "示例播客·离线调试" / "EP01 最早的一集.md").read_text(encoding="utf-8")
    assert note.startswith("---\n")
    assert "author: 主播小张" in note
    assert 'source: "https://example.com/episodes/1"' in note  # URL 含冒号，YAML 加引号
    assert "  - 播客字幕" in note
    assert "  - 示例播客·离线调试" in note

    index = (tmp_path / "示例播客·离线调试" / "示例播客·离线调试.md").read_text(encoding="utf-8")
    assert "season_id:" not in index  # 播客索引页不写 season_id
    assert "  - 播客字幕" in index
    assert "episodes: 2" in index  # 无文稿的 EP03 不入索引
    assert notes.wikilink("EP02 中间的一集", "第2集 中间的一集") in index


def test_podcast_platform_error_counts_as_failure(tmp_path: Path):
    script = {1: PlatformError("HTTP 500：文稿下载失败"), 2: track_of("第二集正常。")}
    client = FakePodcastClient(make_episodes()[:2], script)

    outcome = run_collection("https://example.com/feed.xml", tmp_path, client, note_mode="obsidian")

    statuses = {r.episode.index: r.status for r in outcome.results}
    assert statuses[1] == EpisodeStatus.FAILED
    assert statuses[2] == EpisodeStatus.SUCCESS
    assert "文稿下载失败" in (outcome.results[0].reason or "")


def test_podcast_preview_carries_meta():
    client = FakePodcastClient(make_episodes(), SCRIPT)

    result = preview_first_episode(
        "https://example.com/feed.xml", client, note_mode="obsidian"
    )

    assert result.markdown
    assert result.meta is not None
    assert result.meta.source == "https://example.com/episodes/1"
    assert "播客字幕" in result.meta.tags
    assert format_preview(result)
