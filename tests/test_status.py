"""subtitle-cli-status 单测：默认最近一次、--all、关键词筛选、--only-failed。"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from subtitle_cli import state
from subtitle_cli.bilibili.models import Episode, EpisodeStatus
from subtitle_cli.status import app

runner = CliRunner()


def _save(season_id: str, name: str, rows: list[tuple], updated: str) -> None:
    """写一份状态记录；updated 显式给出，避免同秒写入导致排序不确定。"""
    record = state.CollectionState(
        season_id=season_id, collection_name=name, output_dir=f"D:/out/{name}"
    )
    for index, status, reason in rows:
        state.record_episode(
            record,
            Episode(bvid=f"BV{season_id}-{index}", title=f"标题{index}", index=index),
            status,
            reason,
        )
    path = state.save_collection(record)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["updated"] = updated
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _two_collections() -> None:
    _save(
        "100",
        "老合集",
        [(1, EpisodeStatus.SUCCESS, None), (2, EpisodeStatus.FAILED, "风控 412")],
        "2026-09-01T10:00:00+00:00",
    )
    _save(
        "200",
        "新合集",
        [(1, EpisodeStatus.SUCCESS, None), (2, EpisodeStatus.NO_SUBTITLE, None)],
        "2026-09-24T10:00:00+00:00",
    )


def test_default_shows_only_most_recent_run():
    _two_collections()

    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert "《新合集》" in result.output
    assert "老合集" not in result.output


def test_all_lists_every_collection():
    _two_collections()

    result = runner.invoke(app, ["--all"])

    assert "《新合集》" in result.output
    assert "《老合集》" in result.output


def test_keyword_selects_collection():
    _two_collections()

    result = runner.invoke(app, ["老合集"])

    assert "《老合集》" in result.output
    assert "《新合集》" not in result.output
    assert "风控 412" in result.output


def test_keyword_no_match_hints():
    _two_collections()

    result = runner.invoke(app, ["不存在的东西"])

    assert "没有匹配的记录" in result.output


def test_only_failed_hides_other_rows():
    _two_collections()

    result = runner.invoke(app, ["老合集", "--only-failed"])

    assert "EP02" in result.output and "风控 412" in result.output
    assert "EP01" not in result.output  # 成功的行不列
    assert "重跑同一条提取命令" in result.output


def test_only_failed_skips_collection_without_failures():
    _two_collections()

    result = runner.invoke(app, ["新合集", "--only-failed"])

    assert "没有匹配的记录" in result.output


def test_no_records_prints_hint():
    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "还没有任何运行记录" in result.output


def test_counts_line_lists_only_present_statuses():
    _save("300", "单集合集", [(1, EpisodeStatus.FAILED, "音频下载失败")], "2026-09-24T11:00:00+00:00")

    result = runner.invoke(app, [])

    assert "失败 1" in result.output
    assert "无字幕" not in result.output  # 没出现过就不写
