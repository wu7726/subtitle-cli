"""编排层：解析输入 → 分集列表 → 逐集取字幕 → 落盘 → 汇总（技术方案 §6、§8）。

单集失败不中断整体；连续多集风控则提前终止（技术方案 §5.4）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Protocol

from pydantic import BaseModel

from . import storage
from .bilibili.client import BilibiliError, RiskControlError
from .bilibili.models import Episode, EpisodeResult, EpisodeStatus, SubtitleTrack
from .converter import subtitle_to_markdown
from .config import RISK_ABORT_THRESHOLD


class PlatformClient(Protocol):
    """平台抽象接口（产品文档 §6）：未来加平台 = 新增实现类 + 工厂分支。"""

    def resolve_input(self, raw: str) -> str: ...

    def list_episodes(self, season_id: str) -> tuple[str, list[Episode]]: ...

    def fetch_subtitles(self, episode: Episode) -> SubtitleTrack | None: ...


class RunOutcome(BaseModel):
    """一次运行的完整结果，供汇总与退出码使用。"""

    season_id: str
    collection_name: str
    results: list[EpisodeResult]
    aborted: bool = False  # 是否因连续风控提前终止
    unprocessed: int = 0  # 提前终止时未处理的分集数


def episode_heading(episode: Episode) -> str:
    """Markdown 一级标题：第{N}集 标题；标题自带该序号前缀时不重复。"""
    prefix = f"第{episode.index}集"
    if episode.title.startswith(prefix):
        return episode.title
    return f"{prefix} {episode.title}"


def run_collection(
    raw_input: str,
    output_dir: Path,
    client: PlatformClient,
    *,
    log: Callable[[str], None] = print,
) -> RunOutcome:
    """跑完整流程。输入不合法抛 ValueError（CLI 转为退出码 2）。"""
    season_id = client.resolve_input(raw_input)
    collection_name, episodes = client.list_episodes(season_id)
    log(f"合集《{collection_name}》共 {len(episodes)} 集，输出目录：{output_dir}")

    results: list[EpisodeResult] = []
    consecutive_risk = 0
    aborted = False

    for episode in episodes:
        label = f"EP{episode.index:02d}"
        path = storage.output_path(output_dir, collection_name, episode.index, episode.title)

        if storage.is_downloaded(path):
            results.append(EpisodeResult(episode=episode, status=EpisodeStatus.SKIPPED))
            log(f"{label} 已存在，跳过")
            continue
        if path.exists():
            # 上次运行中断留下的空文件：清掉后正常重下
            path.unlink()

        try:
            track = client.fetch_subtitles(episode)
        except RiskControlError as exc:
            consecutive_risk += 1
            results.append(EpisodeResult(episode=episode, status=EpisodeStatus.FAILED, reason=str(exc)))
            log(f"{label} 失败：{exc}")
            if consecutive_risk >= RISK_ABORT_THRESHOLD:
                aborted = True
                log(f"连续 {consecutive_risk} 集疑似风控，提前终止。建议稍后重跑，已成功分集会自动跳过。")
                break
            continue
        except BilibiliError as exc:
            consecutive_risk = 0
            results.append(EpisodeResult(episode=episode, status=EpisodeStatus.FAILED, reason=str(exc)))
            log(f"{label} 失败：{exc}")
            continue

        consecutive_risk = 0
        if track is None:
            results.append(EpisodeResult(episode=episode, status=EpisodeStatus.NO_SUBTITLE))
            log(f"{label} 无字幕")
            continue

        content = subtitle_to_markdown(episode_heading(episode), track.lines)
        try:
            storage.write_markdown(path, content)
        except OSError as exc:
            results.append(EpisodeResult(episode=episode, status=EpisodeStatus.FAILED, reason=f"写入失败：{exc}"))
            log(f"{label} 失败：写入失败（{exc}）")
            continue
        results.append(EpisodeResult(episode=episode, status=EpisodeStatus.SUCCESS))
        log(f"{label} 成功")

    unprocessed = len(episodes) - len(results)
    return RunOutcome(
        season_id=season_id,
        collection_name=collection_name,
        results=results,
        aborted=aborted,
        unprocessed=max(unprocessed, 0),
    )


def summarize(outcome: RunOutcome) -> str:
    """汇总文本，对齐产品文档三类结果（技术方案 §8）。"""
    results = outcome.results
    skipped = sum(1 for r in results if r.status == EpisodeStatus.SKIPPED)
    success = sum(1 for r in results if r.status == EpisodeStatus.SUCCESS)
    no_subtitle = [r for r in results if r.status == EpisodeStatus.NO_SUBTITLE]
    failed = [r for r in results if r.status == EpisodeStatus.FAILED]

    lines = ["—— 汇总 ——"]
    lines.append(f"成功 {success + skipped}（其中增量跳过 {skipped}）")
    if no_subtitle:
        labels = "、".join(f"EP{r.episode.index:02d}" for r in no_subtitle)
        lines.append(f"无字幕  {len(no_subtitle)}：{labels}")
    if failed:
        labels = "、".join(
            f"EP{r.episode.index:02d}（{r.reason}）" if r.reason else f"EP{r.episode.index:02d}"
            for r in failed
        )
        lines.append(f"失败    {len(failed)}：{labels}")
    if outcome.aborted:
        lines.append(f"注意：因连续风控提前终止，剩余 {outcome.unprocessed} 集未处理。")
    if failed:
        lines.append("失败可重跑：subtitle-cli <同一输入> 会自动跳过已成功分集")
    return "\n".join(lines)


def has_failure(outcome: RunOutcome) -> bool:
    """是否存在失败（决定退出码，技术方案 §8）。"""
    return any(r.status == EpisodeStatus.FAILED for r in outcome.results)
