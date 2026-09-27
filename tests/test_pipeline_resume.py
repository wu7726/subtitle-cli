"""pipeline 断点续跑（优化刀3）：状态决定跳过/重试，--recheck 重查无字幕。

核心不变量：
- 状态是权威 —— 产物被搬走（整理进 daily/nothing）不该触发重新下载
- NO_SUBTITLE 是「查过的结论」，默认不再联网；--recheck 才重查
- FAILED 每次都重试
"""

from __future__ import annotations

from pathlib import Path

from subtitle_cli import state
from subtitle_cli.bilibili.client import BilibiliError, RiskControlError
from subtitle_cli.bilibili.models import Episode, EpisodeStatus
from subtitle_cli.pipeline import run_collection, summarize

from tests.test_pipeline import FakeClient, make_episodes, track_of


def _state_root(tmp_path: Path) -> Path:
    return tmp_path / "_state"


def _run(tmp_path: Path, client: FakeClient, **kwargs):
    return run_collection("100", tmp_path, client, state_root=_state_root(tmp_path), **kwargs)


def test_no_subtitle_episode_not_requeried(tmp_path: Path):
    first = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: None})
    _run(tmp_path, first)
    assert [e.index for e in first.fetched] == [1, 2]

    second = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: track_of("补传。")})
    outcome = _run(tmp_path, second)

    assert second.fetched == []  # 一次网络都没发
    assert [r.status for r in outcome.results] == [
        EpisodeStatus.SKIPPED,
        EpisodeStatus.NO_SUBTITLE,
    ]
    assert "无字幕  1：EP02" in summarize(outcome)


def test_recheck_requeries_no_subtitle(tmp_path: Path):
    first = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: None})
    _run(tmp_path, first)

    second = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: track_of("补传字幕。")})
    outcome = _run(tmp_path, second, recheck=True)

    assert [e.index for e in second.fetched] == [2]  # 只重查了无字幕那集
    assert outcome.results[1].status == EpisodeStatus.SUCCESS
    body = (tmp_path / "测试合集" / "EP02 标题2.md").read_text(encoding="utf-8")
    assert "补传字幕。" in body
    # 状态被更新为成功，下次重跑不会再问
    recorded = state.load_collection("100", tmp_path, _state_root(tmp_path))
    assert recorded.episodes["cid2"].status == EpisodeStatus.SUCCESS


def test_new_output_dir_starts_fresh(tmp_path: Path):
    """换个落点再跑 = 在新地方重新出一份，不能因为「做过」而跳过。

    （共用同一个状态目录，验证的是落点指纹而不是目录隔离。）
    """
    root = _state_root(tmp_path)
    out_a, out_b = tmp_path / "A", tmp_path / "B"

    first = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: track_of("二。")})
    run_collection("100", out_a, first, state_root=root)

    second = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: track_of("二。")})
    outcome = run_collection("100", out_b, second, state_root=root)

    assert [e.index for e in second.fetched] == [1, 2]
    assert all(r.status == EpisodeStatus.SUCCESS for r in outcome.results)
    assert (out_b / "测试合集" / "EP01 标题1.md").is_file()
    # 旧落点的记录仍然完好（两个落点各有一份状态文件）
    assert state.load_collection("100", out_a, root).episodes
    assert len(list(root.glob("*.json"))) == 2


def test_failed_episode_is_retried(tmp_path: Path):
    first = FakeClient(
        episodes=make_episodes(2),
        script={1: BilibiliError("业务错误 -404"), 2: track_of("二。")},
    )
    outcome = _run(tmp_path, first)
    assert outcome.results[0].status == EpisodeStatus.FAILED

    second = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: track_of("二。")})
    outcome = _run(tmp_path, second)

    assert [e.index for e in second.fetched] == [1]  # 只重试失败的那集
    assert outcome.results[0].status == EpisodeStatus.SUCCESS
    assert (tmp_path / "测试合集" / "EP01 标题1.md").is_file()


def test_state_wins_over_missing_product(tmp_path: Path):
    """产物被搬走（整理进别的库）后重跑：不重新下载，不复活文件。"""
    first = FakeClient(episodes=make_episodes(1), script={1: track_of("一。")})
    _run(tmp_path, first)
    written = tmp_path / "测试合集" / "EP01 标题1.md"
    assert written.is_file()
    written.unlink()  # 模拟把笔记搬进 daily/ 或 nothing/

    second = FakeClient(episodes=make_episodes(1), script={1: track_of("一。")})
    outcome = _run(tmp_path, second)

    assert second.fetched == []
    assert outcome.results[0].status == EpisodeStatus.SKIPPED
    assert not written.exists()


def test_historical_product_without_state_still_skips(tmp_path: Path):
    """向后兼容：历史产物没有状态记录，靠产物存在性跳过（旧行为不变）。"""
    existing = tmp_path / "测试合集" / "EP01 标题1.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("旧内容", encoding="utf-8")

    client = FakeClient(episodes=make_episodes(1), script={1: track_of("新内容。")})
    outcome = _run(tmp_path, client)

    assert client.fetched == []
    assert outcome.results[0].status == EpisodeStatus.SKIPPED
    assert existing.read_text(encoding="utf-8") == "旧内容"


def test_resume_after_risk_abort(tmp_path: Path):
    """风控中断后重跑：失败的集重试，未处理的集继续（已成功的按状态跳过）。"""
    eps = make_episodes(7)
    first = FakeClient(
        episodes=eps,
        script={
            1: track_of("一。"),
            **{i: RiskControlError("HTTP 412，疑似风控") for i in range(2, 7)},
        },
    )
    outcome = _run(tmp_path, first)

    assert outcome.aborted is True
    assert len(outcome.results) == 6  # EP01 成功 + 连续 5 集风控后终止
    assert outcome.unprocessed == 1
    assert outcome.results[0].status == EpisodeStatus.SUCCESS

    second = FakeClient(
        episodes=eps, script={i: track_of(f"第{i}集内容。") for i in range(1, 8)}
    )
    outcome = _run(tmp_path, second)

    # EP01 已成功不再重试；EP02..06 重试、EP07 补上
    assert [e.index for e in second.fetched] == [2, 3, 4, 5, 6, 7]
    assert outcome.results[0].status == EpisodeStatus.SKIPPED
    assert all(r.status == EpisodeStatus.SUCCESS for r in outcome.results[1:])
    assert outcome.unprocessed == 0


def test_positional_key_episodes_are_never_skipped(tmp_path: Path):
    """播客 feed 没有稳定单集 ID 时键退化成位置序号 —— 这种键不参与跳过判断。

    feed 增删一集，序号就整体错位，错信记录会把**另一集**的结论当成本集的，
    静默漏掉一集。宁可重新联网。
    """
    def positional_episodes() -> list[Episode]:
        return [Episode(bvid=f"podcast-{i}", title=f"标题{i}", index=i) for i in (1, 2)]

    first = FakeClient(episodes=positional_episodes(), script={1: track_of("一。"), 2: track_of("二。")})
    _run(tmp_path, first)
    # 结论确实记下来了（status 会显示，并标注键不可靠）
    recorded = state.load_collection("100", tmp_path, _state_root(tmp_path))
    assert recorded.episodes["podcast-1"].status == EpisodeStatus.SUCCESS
    assert recorded.episodes["podcast-1"].key_positional is True

    for product in (tmp_path / "测试合集").glob("*.md"):
        product.unlink()  # 产物被搬走，只剩状态记录

    second = FakeClient(episodes=positional_episodes(), script={1: track_of("一。"), 2: track_of("二。")})
    outcome = _run(tmp_path, second)

    assert [e.index for e in second.fetched] == [1, 2]  # 不可靠的键 → 重新联网
    assert all(r.status == EpisodeStatus.SUCCESS for r in outcome.results)


def test_recheck_does_not_touch_successful_episodes(tmp_path: Path):
    """--recheck 只影响无字幕集，已成功的行仍按状态跳过。"""
    first = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: track_of("二。")})
    _run(tmp_path, first)

    second = FakeClient(episodes=make_episodes(2), script={1: track_of("一。"), 2: track_of("二。")})
    _run(tmp_path, second, recheck=True)

    assert second.fetched == []
