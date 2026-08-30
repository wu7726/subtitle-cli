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
from .config import RISK_ABORT_THRESHOLD
from .converter import subtitle_to_markdown
from .reviewer import AuditReport, CleaningStats, audit_markdown, clean_lines, format_report


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
    audit: AuditReport | None = None  # 全部成功分集的审查报告汇总


class PreviewResult(BaseModel):
    """提取前预览：第 1 集的成品 Markdown + 审查报告（技术方案 §7）。"""

    season_id: str
    collection_name: str
    total_episodes: int
    episode_index: int
    episode_title: str
    markdown: str
    audit: AuditReport
    logged_in: bool | None = None
    uname: str | None = None


def _check_login(client: PlatformClient, log: Callable[[str], None]) -> None:
    """登录态自检：B站不向未登录请求返回字幕列表，Cookie 无效必须显式提醒。

    whoami 是 BilibiliClient 的增强能力（协议外可选），其他实现可没有。
    """
    whoami = getattr(client, "whoami", None)
    if whoami is None:
        return
    try:
        logged_in, uname = whoami()
    except Exception:  # noqa: BLE001 - 自检失败不影响主流程
        log("登录态：校验失败（网络或风控），继续尝试提取")
        return
    if logged_in:
        log(f"登录态：已登录（{uname}）")
    else:
        log(
            "⚠️ 未登录：B站不向未登录请求返回字幕列表，本次所有分集都将显示为"
            "「无字幕」。请检查 Cookie 是否为从浏览器复制的完整整串（需含 "
            "SESSDATA=），且未过期。"
        )


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
    _check_login(client, log)

    results: list[EpisodeResult] = []
    consecutive_risk = 0
    aborted = False
    reports: list[AuditReport] = []

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

        # 落盘前审查：保守清洗（无效标记行、连续重复行），并生成排版体检报告
        cleaned, cleaning = clean_lines(track.lines)
        content = subtitle_to_markdown(episode_heading(episode), cleaned)
        reports.append(audit_markdown(content, cleaning))
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
        audit=_aggregate_reports(reports),
    )


def _aggregate_reports(reports: list[AuditReport]) -> AuditReport | None:
    """把逐集审查报告汇总为整卷报告（无成功分集时返回 None）。"""
    if not reports:
        return None
    total_in = sum(r.cleaning.total_in for r in reports if r.cleaning)
    total_out = sum(r.cleaning.total_out for r in reports if r.cleaning)
    total_paragraphs = sum(r.paragraphs for r in reports)
    weighted_avg = (
        round(sum(r.avg_paragraph_chars * r.paragraphs for r in reports) / total_paragraphs)
        if total_paragraphs
        else 0
    )
    return AuditReport(
        paragraphs=total_paragraphs,
        max_paragraph_chars=max((r.max_paragraph_chars for r in reports), default=0),
        avg_paragraph_chars=weighted_avg,
        long_paragraph_count=sum(r.long_paragraph_count for r in reports),
        fragment_count=sum(r.fragment_count for r in reports),
        cleaning=CleaningStats(
            total_in=total_in,
            total_out=total_out,
            removed_fillers=sum(r.cleaning.removed_fillers for r in reports if r.cleaning),
            merged_duplicates=sum(r.cleaning.merged_duplicates for r in reports if r.cleaning),
        ),
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
    if outcome.audit is not None and outcome.audit.paragraphs:
        lines.append(f"审查：{outcome.audit.one_line()}")
    if failed:
        lines.append("失败可重跑：subtitle-cli <同一输入> 会自动跳过已成功分集")
    return "\n".join(lines)


def preview_first_episode(
    raw_input: str,
    client: PlatformClient,
    *,
    log: Callable[[str], None] = print,
) -> PreviewResult:
    """提取前审查：只提取第 1 集，返回成品 Markdown 与审查报告（不写文件）。

    用于在批量提取前确认字幕的排版与内容质量（产品文档 v0.2 增补）。
    第 1 集无字幕时 markdown 为空串，message 说明原因。
    """
    season_id = client.resolve_input(raw_input)
    collection_name, episodes = client.list_episodes(season_id)
    if not episodes:
        raise ValueError("该合集没有任何分集")
    first = episodes[0]
    log(f"预览《{collection_name}》第 1 集：{first.title}")

    track = client.fetch_subtitles(first)
    cleaned, cleaning = clean_lines(track.lines) if track else ([], None)
    markdown = subtitle_to_markdown(episode_heading(first), cleaned)
    audit = audit_markdown(markdown, cleaning)

    logged_in: bool | None = None
    uname: str | None = None
    whoami = getattr(client, "whoami", None)
    if whoami is not None:
        try:
            logged_in, uname = whoami()
        except Exception:  # noqa: BLE001 - 预览的自检失败不影响结果
            pass

    return PreviewResult(
        season_id=season_id,
        collection_name=collection_name,
        total_episodes=len(episodes),
        episode_index=first.index,
        episode_title=first.title,
        markdown=markdown if track else "",
        audit=audit,
        logged_in=logged_in,
        uname=uname,
    )


def format_preview(result: PreviewResult) -> str:
    """预览结果的终端文本形态。"""
    parts = [
        f"合集《{result.collection_name}》共 {result.total_episodes} 集，"
        f"预览第 {result.episode_index} 集：{result.episode_title}"
    ]
    if result.logged_in is False:
        parts.append("⚠️ 未登录：B站不向未登录请求返回字幕列表，无法预览内容。请检查 Cookie。")
    if not result.markdown:
        parts.append("第 1 集没有可用字幕，无法预览；可继续批量提取其余分集。")
    else:
        parts.append(format_report(result.audit))
        parts.append("—— 第 1 集成品预览 ——")
        parts.append(result.markdown.rstrip())
    return "\n\n".join(parts)


def has_failure(outcome: RunOutcome) -> bool:
    """是否存在失败（决定退出码，技术方案 §8）。"""
    return any(r.status == EpisodeStatus.FAILED for r in outcome.results)
