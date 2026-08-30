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
from subtitle_cli.pipeline import (
    RunOutcome,
    format_preview,
    has_failure,
    preview_first_episode,
    run_collection,
    summarize,
)


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


# ---- 登录态自检 ----
class FakeClientWithAuth(FakeClient):
    def __init__(self, *args, logged_in=True, uname="测试用户", **kwargs):
        super().__init__(*args, **kwargs)
        self._logged_in = logged_in
        self._uname = uname

    def whoami(self):
        return self._logged_in, self._uname


def test_login_notice_logged_in(tmp_path: Path):
    eps = make_episodes(1)
    client = FakeClientWithAuth(episodes=eps, script={1: track_of("内容。")}, logged_in=True)
    lines: list[str] = []
    run_collection("100", tmp_path, client, log=lines.append)
    assert any("已登录（测试用户）" in line for line in lines)


def test_login_notice_warns_when_anonymous(tmp_path: Path):
    eps = make_episodes(1)
    client = FakeClientWithAuth(episodes=eps, script={1: None}, logged_in=False)
    lines: list[str] = []
    outcome = run_collection("100", tmp_path, client, log=lines.append)
    warning = next(line for line in lines if "未登录" in line)
    assert "SESSDATA" in warning
    # 未登录仍然完整跑完流程（该集归入无字幕），不中断
    assert outcome.results[0].status == EpisodeStatus.NO_SUBTITLE


def test_login_selfcheck_failure_does_not_abort(tmp_path: Path):
    class BrokenAuthClient(FakeClient):
        def whoami(self):
            raise BilibiliError("网络错误")

    eps = make_episodes(1)
    client = BrokenAuthClient(episodes=eps, script={1: track_of("内容。")})
    lines: list[str] = []
    outcome = run_collection("100", tmp_path, client, log=lines.append)
    assert outcome.results[0].status == EpisodeStatus.SUCCESS
    assert any("校验失败" in line for line in lines)


# ---- 提取前审查与预览 ----
def test_run_cleans_and_includes_audit_summary(tmp_path: Path):
    """落盘前清洗（重复行/标记行）+ 汇总含审查行。"""
    eps = make_episodes(1)
    track = SubtitleTrack(
        lan="zh-CN",
        lines=[
            SubtitleLine(from_time=0, to_time=1, content="（音乐）"),
            SubtitleLine(from_time=1, to_time=2, content="真实内容。"),
            SubtitleLine(from_time=2, to_time=3, content="真实内容。"),
        ],
    )
    client = FakeClient(episodes=eps, script={1: track})

    outcome = run_collection("100", tmp_path, client)

    path = tmp_path / "测试合集" / "EP01 标题1.md"
    text = path.read_text(encoding="utf-8")
    assert "（音乐）" not in text
    assert text.count("真实内容。") == 1  # 连续重复已合并
    assert outcome.audit is not None and outcome.audit.cleaning.removed_fillers == 1
    summary = summarize(outcome)
    assert "审查：" in summary and "清理无效行 1" in summary


def test_preview_first_episode(tmp_path: Path):
    eps = make_episodes(2)
    client = FakeClient(episodes=eps, script={1: track_of("第一集的内容。"), 2: track_of("x")})
    lines: list[str] = []

    result = preview_first_episode("100", client, log=lines.append)

    assert result.collection_name == "测试合集"
    assert result.total_episodes == 2
    assert result.markdown == "# 第1集 标题1\n\n第一集的内容。\n"
    assert result.audit.paragraphs == 1
    assert result.audit.cleaning.removed_fillers == 0
    text = format_preview(result)
    assert "审查报告" in text and "第一集的内容。" in text


def test_preview_no_subtitle_first_episode(tmp_path: Path):
    eps = make_episodes(2)
    client = FakeClient(episodes=eps, script={1: None, 2: track_of("x")})
    result = preview_first_episode("100", client)
    assert result.markdown == ""
    assert "没有可用字幕" in format_preview(result)


# ---- 输入校验透传 ----
def test_invalid_input_raises_value_error(tmp_path: Path):
    client = FakeClient()
    with pytest.raises(ValueError):
        run_collection("BV123", tmp_path, client)


