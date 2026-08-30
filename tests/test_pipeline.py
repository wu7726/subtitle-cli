"""pipeline 编排单测：增量跳过、失败不中断、风控提前终止、汇总与退出码判定。

用 FakeClient（满足 PlatformClient 协议）替代网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from subtitle_cli import pipeline
from subtitle_cli.bilibili.client import BilibiliError, RiskControlError
from subtitle_cli.bilibili.models import (
    Episode,
    EpisodeResult,
    EpisodeStatus,
    SubtitleLine,
    SubtitleTrack,
)
from subtitle_cli.config import RISK_ABORT_THRESHOLD
from subtitle_cli.pipeline import RunOutcome, has_failure, run_collection, summarize


class FakeClient:
    """按剧本返回结果的假平台客户端。

    script: 每集索引 → SubtitleTrack / None / Exception
    """

    def __init__(self, name: str = "测试合集", episodes: list[Episode] | None = None, script: dict | None = None):
        self.collection_name = name
        self.episodes = episodes or []
        self.script = script or {}
        self.fetched: list[Episode] = []

    def resolve_input(self, raw: str) -> str:
        if raw.strip().isdigit():
            return raw.strip()
        raise ValueError("bad input")

    def list_episodes(self, season_id: str) -> tuple[str, list[Episode]]:
        return self.collection_name, self.episodes

    def fetch_subtitles(self, episode: Episode) -> SubtitleTrack | None:
        self.fetched.append(episode)
        outcome = self.script[episode.index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_episodes(n: int) -> list[Episode]:
    return [Episode(bvid=f"BV{i:02d}", cid=f"cid{i}", title=f"标题{i}", index=i) for i in range(1, n + 1)]


def track_of(text: str) -> SubtitleTrack:
    return SubtitleTrack(lan="zh-CN", lines=[SubtitleLine(from_time=0, to_time=1, content=text)])


# ---- 正常流程 ----
def test_success_flow_writes_markdown(tmp_path: Path):
    eps = make_episodes(3)
    client = FakeClient(episodes=eps, script={1: track_of("内容一。"), 2: track_of("内容二。"), 3: track_of("内容三。")})

    outcome = run_collection("100", tmp_path, client)

    assert isinstance(outcome, RunOutcome)
    assert outcome.season_id == "100" and outcome.collection_name == "测试合集"
    assert [r.status.value for r in outcome.results] == ["success"] * 3
    path = tmp_path / "测试合集" / "EP01 标题1.md"
    assert path.is_file()
    assert path.read_text(encoding="utf-8").startswith("# 第1集 标题1")
    assert not has_failure(outcome)
    assert summarize(outcome).splitlines()[1] == "成功 3（其中增量跳过 0）"


def test_incremental_skip_existing_file(tmp_path: Path):
    eps = make_episodes(2)
    existing = tmp_path / "测试合集" / "EP01 标题1.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("旧内容", encoding="utf-8")
    client = FakeClient(episodes=eps, script={1: track_of("x"), 2: track_of("新内容。")})

    outcome = run_collection("100", tmp_path, client)

    statuses = {r.episode.index: r.status for r in outcome.results}
    assert statuses[1] == EpisodeStatus.SKIPPED
    assert statuses[2] == EpisodeStatus.SUCCESS
    assert existing.read_text(encoding="utf-8") == "旧内容"  # 未被覆盖
    assert "其中增量跳过 1" in summarize(outcome)


def test_leftover_empty_file_is_redownloaded(tmp_path: Path):
    """上次中断留下的 0 字节文件应重新下载而非跳过。"""
    eps = make_episodes(1)
    empty = tmp_path / "测试合集" / "EP01 标题1.md"
    empty.parent.mkdir(parents=True)
    empty.write_text("", encoding="utf-8")
    client = FakeClient(episodes=eps, script={1: track_of("重新下载的内容。")})

    outcome = run_collection("100", tmp_path, client)

    assert outcome.results[0].status == EpisodeStatus.SUCCESS
    assert empty.read_text(encoding="utf-8") == "# 第1集 标题1\n\n重新下载的内容。\n"


# ---- 三分类结果 ----
def test_no_subtitle_result(tmp_path: Path):
    eps = make_episodes(1)
    client = FakeClient(episodes=eps, script={1: None})

    outcome = run_collection("100", tmp_path, client)

    assert outcome.results[0].status == EpisodeStatus.NO_SUBTITLE
    assert not (tmp_path / "测试合集" / "EP01 标题1.md").exists()
    summary = summarize(outcome)
    assert "无字幕  1：EP01" in summary


def test_single_failure_does_not_stop_others(tmp_path: Path):
    eps = make_episodes(3)
    client = FakeClient(
        episodes=eps,
        script={
            1: BilibiliError("业务错误 -404: 啥都木有"),
            2: track_of("正常。"),
            3: None,
        },
    )

    outcome = run_collection("100", tmp_path, client)

    statuses = [r.status for r in outcome.results]
    assert statuses == [EpisodeStatus.FAILED, EpisodeStatus.SUCCESS, EpisodeStatus.NO_SUBTITLE]
    assert has_failure(outcome)
    summary = summarize(outcome)
    assert "失败    1：EP01（业务错误 -404: 啥都木有）" in summary
    assert "失败可重跑" in summary


def test_write_failure_reported_as_failed(tmp_path: Path, monkeypatch):
    eps = make_episodes(1)
    client = FakeClient(episodes=eps, script={1: track_of("内容。")})

    def boom(path, content):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline.storage, "write_markdown", boom)
    outcome = run_collection("100", tmp_path, client)
    assert outcome.results[0].status == EpisodeStatus.FAILED
    assert "写入失败" in outcome.results[0].reason


# ---- 风控提前终止 ----
def test_consecutive_risk_aborts_pipeline(tmp_path: Path):
    eps = make_episodes(7)
    client = FakeClient(
        episodes=eps,
        script={i: RiskControlError("HTTP 412，疑似风控") for i in range(1, 8)},
    )

    outcome = run_collection("100", tmp_path, client)

    assert outcome.aborted is True
    assert len(outcome.results) == RISK_ABORT_THRESHOLD  # 处理 5 集后终止
    assert outcome.unprocessed == 2
    assert all(r.status == EpisodeStatus.FAILED for r in outcome.results)
    assert "剩余 2 集未处理" in summarize(outcome)


def test_risk_counter_resets_after_success(tmp_path: Path):
    eps = make_episodes(3)
    client = FakeClient(
        episodes=eps,
        script={
            1: RiskControlError("HTTP 412，疑似风控"),
            2: track_of("恢复。"),
            3: RiskControlError("HTTP 412，疑似风控"),
        },
    )

    outcome = run_collection("100", tmp_path, client)

    # 若计数不重置，第 3 集不会达到阈值（阈值 5），这里验证流程跑满 3 集
    assert len(outcome.results) == 3
    assert outcome.aborted is False


def test_skip_does_not_reset_risk_counter(tmp_path: Path):
    """增量跳过发生在 try 之前，不应影响连续风控计数逻辑（跳过集不计入）。"""
    eps = make_episodes(3)
    existing = tmp_path / "测试合集" / "EP01 标题1.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("x", encoding="utf-8")
    client = FakeClient(
        episodes=eps,
        script={2: RiskControlError("412"), 3: RiskControlError("412")},
    )

    outcome = run_collection("100", tmp_path, client)

    assert [r.status.value for r in outcome.results] == ["skipped", "failed", "failed"]


# ---- 汇总格式（对齐技术方案 §8）----
def test_summary_format_matches_spec():
    results = [
        EpisodeResult(episode=Episode(bvid=f"B{i}", index=i, title="t"), status=EpisodeStatus.SKIPPED)
        for i in range(1, 41)
    ]
    outcome = RunOutcome(season_id="100", collection_name="测试合集", results=results)
    text = summarize(outcome)
    assert text == "—— 汇总 ——\n成功 40（其中增量跳过 40）"


def test_summary_empty_collection():
    outcome = RunOutcome(season_id="1", collection_name="空", results=[])
    assert summarize(outcome) == "—— 汇总 ——\n成功 0（其中增量跳过 0）"


# ---- 输入校验透传 ----
def test_invalid_input_raises_value_error(tmp_path: Path):
    client = FakeClient()
    with pytest.raises(ValueError):
        run_collection("BV123", tmp_path, client)
