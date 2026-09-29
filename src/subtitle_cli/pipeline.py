"""编排层：解析输入 → 分集列表 → 逐集取字幕 → 落盘 → 汇总（技术方案 §6、§8）。

单集失败不中断整体；连续多集风控则提前终止（技术方案 §5.4）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal, Protocol
from collections.abc import Callable

from pydantic import BaseModel

from . import notes, state, storage
from . import asr as asr_module  # 别名：run_collection 的 asr 参数会遮蔽模块名
from .bilibili.client import BilibiliError, RiskControlError
from .bilibili.models import Episode, EpisodeResult, EpisodeStatus, SubtitleTrack
from .config import ASR_MODEL_SIZE, AUDIO_CACHE_HINT_BYTES, RISK_ABORT_THRESHOLD
from .converter import subtitle_to_markdown
from .errors import PlatformError
from .reviewer import AuditReport, CleaningStats, audit_markdown, clean_lines, format_report


class PlatformClient(Protocol):
    """平台抽象接口（产品文档 §6）：未来加平台 = 新增实现类 + 工厂分支。"""

    def resolve_input(self, raw: str) -> str: ...

    def list_episodes(self, season_id: str) -> tuple[str, list[Episode]]: ...

    def fetch_subtitles(self, episode: Episode) -> SubtitleTrack | None: ...

    def download_audio(self, episode: Episode, dest: Path) -> Path: ...

    def __enter__(self) -> PlatformClient: ...

    def __exit__(self, *exc_info: object) -> None: ...


class RunOutcome(BaseModel):
    """一次运行的完整结果，供汇总与退出码使用。"""

    season_id: str
    collection_name: str
    results: list[EpisodeResult]
    aborted: bool = False  # 是否因连续风控提前终止
    unprocessed: int = 0  # 提前终止时未处理的分集数
    audit: AuditReport | None = None  # 全部成功分集的审查报告汇总
    note_mode: Literal["plain", "obsidian"] = "plain"
    output_dir: str = ""  # 落点父目录（obsidian 模式 = <vault>/<subdir>）
    index_path: str | None = None  # 重生成的合集索引页路径（obsidian 模式）
    asr_count: int = 0  # 本次经本地语音转写出正文的集数（汇总展示）


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
    note_mode: Literal["plain", "obsidian"] = "plain"
    meta: notes.EpisodeMeta | None = None  # obsidian 模式的属性头来源


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
            "未登录：B站不向未登录请求返回字幕列表，本次所有分集都将显示为"
            "「无字幕」。请检查 Cookie 是否为从浏览器复制的完整整串（需含 "
            "SESSDATA=），且未过期。"
        )


def _base_tag_of(client: PlatformClient) -> str:
    """平台基础标签（播客「播客字幕」；未声明的实现回落B站标签）。"""
    return getattr(client, "base_tag", "") or notes.BASE_TAG


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
    note_mode: Literal["plain", "obsidian"] = "plain",
    fetched_at: date | None = None,
    asr: bool = False,
    asr_model: str = ASR_MODEL_SIZE,
    asr_limit: int | None = None,
    state_root: Path | None = None,
    recheck: bool = False,
    audio_cache: bool = True,
) -> RunOutcome:
    """跑完整流程。输入不合法抛 ValueError（CLI 转为退出码 2）。

    note_mode="obsidian"：成功分集包上属性头（PRD F2），运行结束重生成
    合集索引页（PRD F3）；正文仍由 converter 产出，一字不动。
    asr=True：无现成字幕/文稿的分集下载音频走本地语音转写兜底
    （多平台扩展计划 §3 第 2 步）；依赖未安装时抛 AsrDependencyError。
    state_root：状态文件落点（默认 ~/.subtitle-cli/runs/）；每集结束立即
    落盘，进程中断也不丢已完成的结论。
    recheck=True：已确认「无字幕」的分集也重新联网查一遍（默认跳过——
    那是查过的结论，不是没做过）。
    audio_cache=False：转写不走音频缓存，每次都重新下载。
    """
    season_id = client.resolve_input(raw_input)
    collection_name, episodes = client.list_episodes(season_id)
    fetched = fetched_at or date.today()
    author = _uploader_of(client, episodes)
    collection_state = state.load_collection(season_id, output_dir, state_root)
    collection_state.collection_name = collection_name
    collection_state.output_dir = str(Path(output_dir).resolve())
    log(f"合集《{collection_name}》共 {len(episodes)} 集，输出目录：{output_dir}")
    _check_login(client, log)

    if asr:
        asr_module.ensure_dependency()  # 提前失败：避免下载完音频才发现缺依赖
        if getattr(client, "download_audio", None) is None:
            log("当前平台不支持音频下载，转写兜底不生效，无字幕的分集将保持无字幕")
            asr = False
        elif audio_cache:
            _log_audio_cache(log)
    elif getattr(client, "platform", "") == "douyin":
        log("提示：抖音没有可直接抓取的字幕，加 --asr 开启本地语音转写才能出正文。")

    results: list[EpisodeResult] = []
    consecutive_risk = 0
    aborted = False
    reports: list[AuditReport] = []
    asr_count = 0

    def _finish(episode: Episode, status: EpisodeStatus, reason: str | None = None) -> None:
        """登记一集的结果：写内存 + 立即原子落盘，中断也不丢已完成的结论。"""
        results.append(EpisodeResult(episode=episode, status=status, reason=reason))
        state.record_episode(collection_state, episode, status, reason)
        try:
            state.save_collection(collection_state, state_root)
        except OSError as exc:
            # 状态是辅助记录，写不进去不该毁掉本次提取
            log(f"状态记录写入失败（不影响本次提取）：{exc}")

    for episode in episodes:
        label = f"EP{episode.index:02d}"
        path = storage.output_path(output_dir, collection_name, episode.index, episode.title)

        # 位置序号兜底的键（播客 feed 没有稳定单集 ID 时）不参与跳过判断：feed
        # 增删一集，序号就会整体错位，错信它会把**另一集**的结论当成这集的，
        # 静默漏掉一集。宁可重新联网，也不拿不可靠的键当依据。
        recorded = (
            None if state.key_is_positional(episode)
            else state.status_of(collection_state, episode)
        )
        if recorded in (EpisodeStatus.SUCCESS, EpisodeStatus.SKIPPED):
            # 状态是权威：产物可能已被搬走（比如你把它整理进了 daily/nothing），
            # 不该因为落点空了就重新下载一遍
            results.append(EpisodeResult(episode=episode, status=EpisodeStatus.SKIPPED))
            log(f"{label} 已处理过，跳过")
            continue
        if recorded == EpisodeStatus.NO_SUBTITLE and not recheck:
            results.append(EpisodeResult(episode=episode, status=EpisodeStatus.NO_SUBTITLE))
            log(f"{label} 上次已确认无字幕，跳过（要重查加 --recheck）")
            continue
        if storage.is_downloaded(path):
            # 状态表出现之前产出的笔记没有记录，只能靠产物文件兜底。别删这条：
            # 删了那批历史笔记会被当成没做过，全部重下一遍。
            _finish(episode, EpisodeStatus.SKIPPED)
            log(f"{label} 已存在，跳过")
            continue
        if path.exists():
            # 上次运行中断留下的空文件：清掉后正常重下
            path.unlink()

        try:
            track = client.fetch_subtitles(episode)
        except RiskControlError as exc:
            consecutive_risk += 1
            _finish(episode, EpisodeStatus.FAILED, str(exc))
            log(f"{label} 失败：{exc}")
            if consecutive_risk >= RISK_ABORT_THRESHOLD:
                aborted = True
                log(f"连续 {consecutive_risk} 集疑似风控，提前终止。建议稍后重跑，已成功分集会自动跳过。")
                break
            continue
        except BilibiliError as exc:
            consecutive_risk = 0
            _finish(episode, EpisodeStatus.FAILED, str(exc))
            log(f"{label} 失败：{exc}")
            continue
        except PlatformError as exc:
            # 非B站平台（播客等）的接口错误，按普通失败处理
            consecutive_risk = 0
            _finish(episode, EpisodeStatus.FAILED, str(exc))
            log(f"{label} 失败：{exc}")
            continue

        consecutive_risk = 0
        if track is None and asr:
            if asr_limit is not None and asr_count >= asr_limit:
                _finish(episode, EpisodeStatus.NO_SUBTITLE)
                log(f"{label} 无字幕（已达本次转写上限 {asr_limit} 集，未转写）")
                continue
            try:
                track = _asr_fallback(client, episode, asr_model, log, cache=audio_cache)
                asr_count += 1
            except PlatformError as exc:
                _finish(episode, EpisodeStatus.FAILED, str(exc))
                log(f"{label} 失败：{exc}")
                continue
        if track is None:
            _finish(episode, EpisodeStatus.NO_SUBTITLE)
            log(f"{label} 无字幕")
            continue

        # 落盘前审查：保守清洗（无效标记行、连续重复行），并生成排版体检报告。
        # 审查对象永远是正文（不含属性头），保证两种模式的排版统计口径一致
        cleaned, cleaning = clean_lines(track.lines)
        body = subtitle_to_markdown(episode_heading(episode), cleaned)
        content = body
        if note_mode == "obsidian":
            content = notes.build_episode_note(
                _episode_meta(
                    collection_name, season_id, episode, fetched, author,
                    base_tag=_base_tag_of(client),
                ),
                body,
            )
        reports.append(audit_markdown(body, cleaning))
        try:
            storage.write_markdown(path, content)
        except OSError as exc:
            _finish(episode, EpisodeStatus.FAILED, f"写入失败：{exc}")
            log(f"{label} 失败：写入失败（{exc}）")
            continue
        _finish(episode, EpisodeStatus.SUCCESS)
        if note_mode == "obsidian":
            log(f"已写入 vault：{path.name}")
        else:
            log(f"{label} 成功")

    index_path = None
    if note_mode == "obsidian":
        # 多P（BV 开头）与播客（feed 地址）不写 season_id 进索引页
        index_season = season_id if season_id.isdigit() else None
        index_path = storage.write_collection_index(
            Path(output_dir) / storage.collection_dirname(collection_name),
            collection_name,
            index_season,
            fetched,
            log,
            base_tag=_base_tag_of(client),
        )

    unprocessed = len(episodes) - len(results)
    return RunOutcome(
        season_id=season_id,
        collection_name=collection_name,
        results=results,
        aborted=aborted,
        unprocessed=max(unprocessed, 0),
        audit=_aggregate_reports(reports),
        note_mode=note_mode,
        output_dir=str(output_dir),
        index_path=str(index_path) if index_path else None,
        asr_count=asr_count,
    )


def _asr_fallback(
    client: PlatformClient,
    episode: Episode,
    asr_model: str,
    log: Callable[[str], None],
    *,
    cache: bool = True,
) -> SubtitleTrack:
    """无字幕分集的语音转写兜底：取音频（缓存优先）→ faster-whisper → 字幕行。

    音频落 ~/.subtitle-cli/cache/：**转写成功即删**，所以留下的只有失败的集，
    重跑直接从缓存转写、不再下载（cache=False 则每次都重新下载）。
    任何环节失败抛 PlatformError（该集计 FAILED），不中断整体流程。
    """
    label = f"EP{episode.index:02d}"
    log(f"{label} 无字幕，转本地语音转写（模型 {asr_model}，可能较慢）…")
    download = client.download_audio
    audio_path = state.audio_cache_path(episode)
    if cache and storage.is_downloaded(audio_path):
        log(f"{label} 音频命中缓存（{audio_path.stat().st_size // 1024} KB），跳过下载")
    else:
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        # 先写 .part 再整体换名：三个平台都是流式写盘，中途 Ctrl-C 会在目标路径
        # 留下半截文件。而缓存命中只判「存在且非空」——半截文件会被当成有效缓存
        # 永远不再重下，转写必然失败且只能手工删文件才能恢复。
        staging = audio_path.with_suffix(".bin.part")
        try:
            download(episode, staging)
        except BaseException:
            staging.unlink(missing_ok=True)  # Ctrl-C 也算在内，别留半截
            raise
        staging.replace(audio_path)
        log(f"{label} 音频已下载（{audio_path.stat().st_size // 1024} KB），开始转写")
    try:
        lines = asr_module.transcribe_audio(audio_path, model_size=asr_model, log=log)
        if not lines:
            raise PlatformError(f"语音转写结果为空（可能无人声或纯音乐）：{episode.title}")
    except Exception:
        if not cache:
            audio_path.unlink(missing_ok=True)
        raise  # 缓存开着就留着音频：重跑不用重新下载
    audio_path.unlink(missing_ok=True)
    log(f"{label} 转写完成：{len(lines)} 段")
    return SubtitleTrack(lan="asr", lines=lines)


def _log_audio_cache(log: Callable[[str], None]) -> None:
    """转写前报一次缓存落点与占用：失败的音频留在这里，可随时整目录删。"""
    directory = state.audio_cache_root()
    try:
        total = sum(p.stat().st_size for p in directory.glob("*.bin"))
    except OSError:
        total = 0
    log(f"音频缓存：{directory}（当前 {total / 1048576:.1f} MB；转写成功即删，失败的留着供重跑）")
    if total > AUDIO_CACHE_HINT_BYTES:
        log(f"提示：缓存已超 {AUDIO_CACHE_HINT_BYTES / 1073741824:.0f} GB，可直接删除该目录清理")


def _uploader_of(client: PlatformClient, episodes: list[Episode]) -> str:
    """合集 UP 主昵称（取第 1 集 bvid 反查，全合集同主；协议外可选能力）。

    uploader_name 是 BilibiliClient 的增强（如 whoami），其他实现可没有；
    任何失败都只让 author 留空，不阻断提取。
    """
    uploader = getattr(client, "uploader_name", None)
    if uploader is None or not episodes:
        return ""
    try:
        return uploader(episodes[0].bvid) or ""
    except Exception:  # noqa: BLE001 - 属性头辅助信息，失败不影响主流程
        return ""


def _episode_meta(
    collection_name: str,
    season_id: str,
    episode: Episode,
    fetched: date,
    author: str = "",
    base_tag: str = notes.BASE_TAG,
) -> notes.EpisodeMeta:
    """一集的属性头来源；多P 以 season_id 以 BV 开头为判定（开发计划 §2.1）。

    属性键对齐 Obsidian Web Clipper 模板：author/created/source/tags/title。
    播客等自带单集链接的平台优先用 episode.source_url。
    """
    is_multi_p = season_id.startswith("BV")
    source = episode.source_url or notes.episode_url(
        episode.bvid, episode.index, is_multi_p=is_multi_p
    )
    return notes.EpisodeMeta(
        title=episode_heading(episode),
        source=source,
        author=author,
        created=fetched,
        published=episode.published,
        description=episode.description,
        tags=notes.episode_tags(collection_name, base_tag),
        collection=collection_name,
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
    if outcome.asr_count:
        lines.append(
            f"成功 {success + skipped}（其中增量跳过 {skipped}、本地转写 {outcome.asr_count}）"
        )
    else:
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
    if outcome.note_mode == "obsidian" and outcome.output_dir:
        lines.append(
            f"已写入 vault："
            f"{Path(outcome.output_dir) / storage.collection_dirname(outcome.collection_name)}"
        )
    if failed:
        lines.append("失败可重跑：subtitle-cli <同一输入> 会自动跳过已成功分集")
    return "\n".join(lines)


def preview_first_episode(
    raw_input: str,
    client: PlatformClient,
    *,
    log: Callable[[str], None] = print,
    note_mode: Literal["plain", "obsidian"] = "plain",
) -> PreviewResult:
    """提取前审查：只提取第 1 集，返回成品 Markdown 与审查报告（不写文件）。

    用于在批量提取前确认字幕的排版与内容质量（产品文档 v0.2 增补）。
    第 1 集无字幕时 markdown 为空串，message 说明原因。
    obsidian 模式下预览稿带属性头（PRD F7），meta 随结果返回供报告核验。
    """
    season_id = client.resolve_input(raw_input)
    collection_name, episodes = client.list_episodes(season_id)
    if not episodes:
        raise ValueError("该合集没有任何分集")
    first = episodes[0]
    log(f"预览《{collection_name}》第 1 集：{first.title}")

    track = client.fetch_subtitles(first)
    cleaned, cleaning = clean_lines(track.lines) if track else ([], None)
    body = subtitle_to_markdown(episode_heading(first), cleaned)
    markdown = body
    meta: notes.EpisodeMeta | None = None
    if note_mode == "obsidian" and track:
        meta = _episode_meta(
            collection_name, season_id, first, date.today(),
            _uploader_of(client, episodes),
            base_tag=_base_tag_of(client),
        )
        markdown = notes.build_episode_note(meta, body)
    audit = audit_markdown(body, cleaning)

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
        note_mode=note_mode,
        meta=meta,
    )


def format_preview(result: PreviewResult) -> str:
    """预览结果的终端文本形态。"""
    parts = [
        f"合集《{result.collection_name}》共 {result.total_episodes} 集，"
        f"预览第 {result.episode_index} 集：{result.episode_title}"
    ]
    if result.logged_in is False:
        parts.append("未登录：B站不向未登录请求返回字幕列表，无法预览内容。请检查 Cookie。")
    if not result.markdown:
        parts.append(
            "第 1 集没有可用字幕，无法预览；可继续批量提取其余分集"
            "（提取时可加 --asr 开启本地语音转写兜底）。"
        )
    else:
        parts.append(format_report(result.audit))
        if result.meta is not None:
            missing = notes.validate_meta(result.meta)
            parts.append(
                "属性头字段：完整"
                if not missing
                else "属性头字段：缺失 " + "、".join(missing)
            )
        parts.append("—— 第 1 集成品预览 ——")
        parts.append(result.markdown.rstrip())
    return "\n\n".join(parts)


def has_failure(outcome: RunOutcome) -> bool:
    """是否存在失败（决定退出码，技术方案 §8）。"""
    return any(r.status == EpisodeStatus.FAILED for r in outcome.results)
