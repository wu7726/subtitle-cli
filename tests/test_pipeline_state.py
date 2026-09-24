"""pipeline 状态记录（优化刀2）：每集结论立即落盘，含 NO_SUBTITLE。

只验证"记录了"；跳过/重跑语义在刀3 接入，本文件不改跳过行为。
"""

from __future__ import annotations

from pathlib import Path

from subtitle_cli import pipeline, state
from subtitle_cli.bilibili.client import BilibiliError
from subtitle_cli.bilibili.models import EpisodeStatus
from subtitle_cli.pipeline import run_collection

from tests.test_pipeline import FakeClient, make_episodes, track_of


def _state_root(tmp_path: Path) -> Path:
    return tmp_path / "_state"


def _load(tmp_path: Path):
    return state.load_collection("100", _state_root(tmp_path))


def test_records_success_and_failure(tmp_path: Path):
    client = FakeClient(
        episodes=make_episodes(3),
        script={
            1: track_of("一。"),
            2: BilibiliError("业务错误 -404: 啥都木有"),
            3: None,
        },
    )

    run_collection("100", tmp_path, client, state_root=_state_root(tmp_path))

    loaded = _load(tmp_path)
    assert loaded.collection_name == "测试合集"
    assert loaded.episodes["cid1"].status == EpisodeStatus.SUCCESS
    assert loaded.episodes["cid2"].status == EpisodeStatus.FAILED
    assert "-404" in (loaded.episodes["cid2"].reason or "")
    # 无字幕是个结论，必须落盘（否则每次重跑都要重新联网问一遍）
    assert loaded.episodes["cid3"].status == EpisodeStatus.NO_SUBTITLE
    assert loaded.episodes["cid1"].title == "标题1"
    assert loaded.episodes["cid1"].index == 1


def test_records_skipped_from_existing_file(tmp_path: Path):
    existing = tmp_path / "测试合集" / "EP01 标题1.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("旧内容", encoding="utf-8")
    client = FakeClient(episodes=make_episodes(1), script={})

    run_collection("100", tmp_path, client, state_root=_state_root(tmp_path))

    assert _load(tmp_path).episodes["cid1"].status == EpisodeStatus.SKIPPED


def test_state_written_before_next_episode_starts(tmp_path: Path):
    """中断安全：EP01 的结论必须在 EP02 开始处理前就已落盘。"""
    client = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: None})
    snapshots: list[set[str]] = []

    def _log(line: str) -> None:
        if line.endswith("无字幕"):
            snapshots.append(set(_load(tmp_path).episodes))

    run_collection("100", tmp_path, client, log=_log, state_root=_state_root(tmp_path))

    # 处理 EP02 时，EP01 的记录已经在磁盘上
    assert snapshots and "cid1" in snapshots[-1]


def test_state_accumulates_across_runs(tmp_path: Path):
    """重跑是增量：新一集的记录追加，旧记录不被清空。"""
    root = _state_root(tmp_path)
    first = FakeClient(episodes=make_episodes(1), script={1: track_of("一。")})
    run_collection("100", tmp_path, first, state_root=root)

    second = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: track_of("二。")})
    run_collection("100", tmp_path, second, state_root=root)

    loaded = state.load_collection("100", root)
    assert set(loaded.episodes) == {"cid1", "cid2"}
    assert loaded.episodes["cid2"].status == EpisodeStatus.SUCCESS


def test_state_write_failure_does_not_break_run(tmp_path: Path, monkeypatch):
    """状态是辅助记录：写不进去只告警，提取照常完成。"""

    def boom(_state, _root=None):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline.state, "save_collection", boom)
    client = FakeClient(episodes=make_episodes(1), script={1: track_of("一。")})
    logs: list[str] = []

    outcome = run_collection(
        "100", tmp_path, client, log=logs.append, state_root=_state_root(tmp_path)
    )

    assert outcome.results[0].status == EpisodeStatus.SUCCESS
    assert (tmp_path / "测试合集" / "EP01 标题1.md").is_file()
    assert any("状态记录写入失败" in line for line in logs)


def test_state_dir_isolated_from_real_home(tmp_path: Path):
    """未显式给 state_root 时走 SUBTITLE_CLI_STATE_DIR（conftest 已指向 tmp）。"""
    client = FakeClient(episodes=make_episodes(1), script={1: track_of("一。")})

    run_collection("100", tmp_path, client)

    from subtitle_cli.state import state_root

    assert str(tmp_path) in str(state_root())
