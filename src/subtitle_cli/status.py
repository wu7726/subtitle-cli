"""subtitle-cli-status 入口：查看运行状态，不用翻滚屏找失败集。

独立 console script（同 subtitle-cli-migrate 的先例）：往主命令上加子命令会让
Typer 失去「单命令特判」，`subtitle-cli <来源>` 就得改口成
`subtitle-cli main <来源>`——破坏现有用法。退出码恒为 0（这是查询命令）。
"""

from __future__ import annotations

from typing import Optional

import typer

from . import state
from .bilibili.models import EpisodeStatus
from .stdio import force_utf8_stdio

app = typer.Typer(
    add_completion=False,
    help="查看字幕提取状态：哪些集成功、无字幕、失败。记录来自最近一次运行。",
)

_LABELS = {
    EpisodeStatus.SUCCESS: "成功",
    EpisodeStatus.SKIPPED: "跳过",
    EpisodeStatus.NO_SUBTITLE: "无字幕",
    EpisodeStatus.FAILED: "失败",
}


def render(record: state.CollectionState, only_failed: bool = False) -> str | None:
    """一份状态记录的文本；无失败集且 only_failed 时返回 None（不打印）。"""
    episodes = sorted(record.episodes.values(), key=lambda e: e.index)
    if only_failed:
        episodes = [e for e in episodes if e.status == EpisodeStatus.FAILED]
        if not episodes:
            return None
    lines = [
        f"《{record.collection_name or record.season_id}》共 {len(record.episodes)} 集"
        f"（season {record.season_id}）",
        f"落点  {record.output_dir or '未记录'}",
        f"更新  {record.updated}",
    ]
    for e in episodes:
        line = f"  [{_LABELS[e.status]}] EP{e.index:02d} {e.title}"
        if e.reason:
            line += f"  — {e.reason}"
        if e.key_positional:
            line += "  ［这条记录的键不可靠：播客源没有稳定单集 ID］"
        lines.append(line)
    lines.append("—— " + "、".join(_counts(record.episodes.values())))
    return "\n".join(lines)


def _counts(episodes) -> list[str]:
    """各状态计数，只列出现过的（没有失败就不写「失败 0」）。"""
    tally: dict[EpisodeStatus, int] = {}
    for e in episodes:
        tally[e.status] = tally.get(e.status, 0) + 1
    order = [EpisodeStatus.SUCCESS, EpisodeStatus.SKIPPED, EpisodeStatus.NO_SUBTITLE,
             EpisodeStatus.FAILED]
    return [f"{_LABELS[s]} {tally[s]}" for s in order if s in tally]


@app.command()
def main(
    collection: Optional[str] = typer.Argument(
        None, help="只看名字或 ID 含该关键词的合集（省略 = 最近一次运行）"
    ),
    all_: bool = typer.Option(False, "--all", help="列出全部合集的记录"),
    only_failed: bool = typer.Option(False, "--only-failed", help="只列失败的集"),
) -> None:
    """列出每集的状态与失败原因。"""
    force_utf8_stdio()
    records = state.iter_collections()
    if not records:
        typer.echo(f"还没有任何运行记录（{state.state_root()}）")
        return
    if collection:
        keyword = collection.casefold()
        records = [r for r in records if keyword in f"{r.season_id} {r.collection_name}".casefold()]
    elif not all_:
        records = records[:1]  # 默认最近一次（iter_collections 按更新时间倒序）

    blocks = [b for b in (render(r, only_failed) for r in records) if b]
    if not blocks:
        hint = "" if (all_ or collection) else "，用 --all 看全部合集"
        typer.echo(f"没有匹配的记录{hint}")
        return
    typer.echo("\n\n".join(blocks))
    if sum(1 for r in records for e in r.episodes.values() if e.status == EpisodeStatus.FAILED):
        typer.echo("重跑同一条提取命令即可重试失败的集（已成功的会自动跳过）")


if __name__ == "__main__":
    app()
