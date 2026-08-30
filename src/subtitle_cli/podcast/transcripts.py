"""文稿文件解析：VTT / SRT / JSON / 纯文本 → 统一字幕行。

纯函数、无 I/O（技术方案 §7 同款约定）。时间戳仅用于 converter 的
"说话停顿换段"启发式，文本内容不做任何改写——清洗仍由 reviewer 统一处理。
"""

from __future__ import annotations

import json
import re

from ..bilibili.models import SubtitleLine

# 一行的起始秒：纯文本无时间戳，用均匀递增的假时间轴，
# 行距 1 秒小于"停顿换段"阈值（2s），段落只按句末标点闭合
_PLAIN_TICK_SECONDS = 1.0

_TIMING = re.compile(
    r"(?P<from>\d{1,3}(?::\d{1,2}){0,2}[.,]\d{1,3}|\d{1,3}(?::\d{1,2}){1,2})"
    r"\s*-->\s*"
    r"(?P<to>\d{1,3}(?::\d{1,2}){0,2}[.,]\d{1,3}|\d{1,3}(?::\d{1,2}){1,2})"
)

# Podcasting 2.0 官方 transcript JSON：{"segments": [{"start": .., "end": .., "text": ..}]}
# 兼容顶层数组与 B站字幕 JSON 形态（{"body": [{"from","to","content"}]}）


def parse_transcript(content: str, mime: str) -> list[SubtitleLine]:
    """按 MIME 分派解析；text/plain 与未知类型先嗅探强信号再兜底纯文本。

    现实中大量 VTT/SRT 文稿被托管方错误标注为 text/plain，因此内容嗅探
    （WEBVTT 头、-->" 时间轴）优先于宽松 MIME。
    """
    normalized = (mime or "").lower()
    if "vtt" in normalized:
        return parse_vtt(content)
    if "srt" in normalized:
        return parse_srt(content)
    if "json" in normalized:
        return parse_json_transcript(content)
    if "html" in normalized or "xml" in normalized:
        return parse_plain(_strip_tags(content))
    head = content.lstrip()[:200]
    if head.startswith("WEBVTT"):
        return parse_vtt(content)
    if _TIMING.search(head):
        return parse_srt(content)
    if head.startswith("{") or head.startswith("["):
        return parse_json_transcript(content)
    return parse_plain(content)


def parse_timestamp(raw: str) -> float:
    """HH:MM:SS.mmm / MM:SS.mmm / SS.mmm → 秒（逗号小数点兼容）。"""
    parts = raw.strip().replace(",", ".").split(":")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + (float(part) if part else 0.0)
    return seconds


def parse_vtt(text: str) -> list[SubtitleLine]:
    """WebVTT：跳过 NOTE/STYLE/REGION 块与 cue 标识行，取 cue 时间与文本。"""
    lines: list[SubtitleLine] = []
    current: list[str] = []
    span: tuple[float, float] | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            if span is not None and current:
                lines.append(_line(*span, " ".join(current)))
            span, current = None, []
            continue
        if span is None:
            if line.strip().startswith(("NOTE", "STYLE", "REGION")):
                continue  # 元数据块：整块丢弃，直到空行
            timing = _TIMING.search(line)
            if timing:
                span = (parse_timestamp(timing.group("from")), parse_timestamp(timing.group("to")))
            continue  # WEBVTT 头或 cue 标识行
        current.append(line.strip())
    if span is not None and current:
        lines.append(_line(*span, " ".join(current)))
    return lines


def parse_srt(text: str) -> list[SubtitleLine]:
    """SubRip：序号行 + 时间行 + 文本行，空行分块。"""
    lines: list[SubtitleLine] = []
    current: list[str] = []
    span: tuple[float, float] | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            if span is not None and current:
                lines.append(_line(*span, " ".join(current)))
            span, current = None, []
            continue
        timing = _TIMING.search(line)
        if timing:
            span = (parse_timestamp(timing.group("from")), parse_timestamp(timing.group("to")))
            continue
        if span is not None:
            current.append(line.strip())
    if span is not None and current:
        lines.append(_line(*span, " ".join(current)))
    return lines


def parse_json_transcript(text: str) -> list[SubtitleLine]:
    """JSON 文稿：segments 数组（官方格式）、顶层数组、或 B站 body 形态。"""
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    segments: list = []
    if isinstance(payload, list):
        segments = payload
    elif isinstance(payload, dict):
        segments = payload.get("segments") or payload.get("body") or []
    lines: list[SubtitleLine] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        start = _number(seg, ("from", "start", "startTime"))
        end = _number(seg, ("to", "end", "endTime"), fallback=start)
        content = str(seg.get("text") or seg.get("content") or "").strip()
        if not content:
            continue
        lines.append(_line(start, end, content))
    return lines


def parse_plain(text: str) -> list[SubtitleLine]:
    """纯文本：逐行成段，假时间轴均匀递增。空行也保留（说话可能有停顿）。"""
    lines: list[SubtitleLine] = []
    for i, raw in enumerate(text.splitlines()):
        content = raw.strip()
        if not content:
            continue
        start = float(i) * _PLAIN_TICK_SECONDS
        lines.append(_line(start, start + _PLAIN_TICK_SECONDS, content))
    return lines


def _strip_tags(html: str) -> str:
    """HTML 文稿兜底：剥标签，块级标签换行防粘连。"""
    text = re.sub(r"(?i)</(p|div|br|h[1-6]|li)>", "\n", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text


def _line(from_time: float, to_time: float, content: str) -> SubtitleLine:
    return SubtitleLine(from_time=from_time, to_time=to_time, content=content)


def _number(seg: dict, keys: tuple[str, ...], fallback: float = 0.0) -> float:
    for key in keys:
        value = seg.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return fallback
