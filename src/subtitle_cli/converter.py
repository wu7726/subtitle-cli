"""字幕 JSON → Markdown 转换层。

纯函数、确定性、无 I/O（技术方案 §7），所有行为用离线 fixture 单测锁定。
段落拼接是刻意简单的 v1 启发式，追求"通顺可读"而非完美分段。
"""

from __future__ import annotations

from . import config
from .bilibili.models import SubtitleLine

# 句末标点：段落达到最小长度且当前行以其结尾时闭合段落
_SENTENCE_ENDINGS = "。！？…"


def _normalize(text: str) -> str:
    """空白归一化：去首尾空格、合并连续空白。"""
    return " ".join(text.split())


def subtitle_to_markdown(title: str, lines: list[SubtitleLine]) -> str:
    """把一条字幕轨转换为 Markdown 字符串。

    规则（技术方案 §7）：
    1. 顺序累积字幕行；行文本先做空白归一化。
    2. 闭合段落条件（满足其一）：
       - 段长 ≥ 80 字符且当前行以句末标点（。！？…）结尾；
       - 当前行与上一行的起点间隔 > 2.0 秒（说话停顿）。
    3. 标题永远作一级标题；段间空行分隔；结尾带一个换行。
    """
    paragraphs: list[str] = []
    current: list[str] = []
    prev_start: float | None = None

    def flush() -> None:
        nonlocal current
        if current:
            paragraphs.append("".join(current))
            current = []

    for line in lines:
        text = _normalize(line.content)
        if not text:
            continue
        if (
            current
            and prev_start is not None
            and line.from_time - prev_start > config.PARAGRAPH_PAUSE_SECONDS
        ):
            flush()
        current.append(text)
        if (
            len("".join(current)) >= config.PARAGRAPH_MIN_CHARS
            and text[-1] in _SENTENCE_ENDINGS
        ):
            flush()
        prev_start = line.from_time
    flush()

    parts = [f"# {title}", *paragraphs]
    return "\n\n".join(parts) + "\n"
