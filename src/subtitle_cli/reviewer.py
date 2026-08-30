"""字幕审查：清洗与排版体检（纯函数，无 I/O，技术方案 §7）。

在落盘/预览前对字幕做**保守清洗**——只删除明确的无效内容，
不动任何正常文本，保证内容准确性；同时对分段排版做体检，
生成审查报告，供"提取前预览"与结果汇总展示。
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from .bilibili.models import SubtitleLine

# 整行都是声音/氛围标记的内容（如 "（音乐）"、"[掌声]"、"♪"）
_FILLER_LINE = re.compile(
    r"^[\s]*(?:[\[\(（【]\s*(?:音乐|歌曲|掌声|笑声|欢呼|鼓掌|鸟叫|鸟鸣|风声|雨声|雷声"
    r"|枪声|爆炸|音效|哼唱|清唱|旁白|静音|开场|结尾)(?:声)?\s*[\]\)）】]|♪+)[\s]*$"
)
# 行内夹杂的标记（删除标记保留其余文字）
_INLINE_MARK = re.compile(
    r"♪+|[\[\(（【]\s*(?:音乐|掌声|笑声|音效|歌曲)(?:声)?\s*[\]\)）】]"
)


class CleaningStats(BaseModel):
    """清洗统计（内容准确性审查的量化结果）。"""

    total_in: int = 0
    total_out: int = 0
    removed_fillers: int = 0  # 纯标记行 / 清理后无实义的行
    merged_duplicates: int = 0  # 连续重复行合并数


class AuditReport(BaseModel):
    """排版审查报告（对渲染后的 Markdown 段落做体检）。"""

    paragraphs: int = 0
    max_paragraph_chars: int = 0
    avg_paragraph_chars: int = 0
    long_paragraph_count: int = 0  # 超长段（> 500 字未分段，阅读体验差）
    fragment_count: int = 0  # 碎段（< 15 字，分段过碎）
    cleaning: CleaningStats | None = None

    def one_line(self) -> str:
        parts = [f"正文 {self.paragraphs} 段，最长 {self.max_paragraph_chars} 字"]
        if self.long_paragraph_count:
            parts.append(f"超长段 {self.long_paragraph_count} 个")
        if self.fragment_count:
            parts.append(f"碎段 {self.fragment_count} 个")
        if self.cleaning and (self.cleaning.removed_fillers or self.cleaning.merged_duplicates):
            parts.append(
                f"清理无效行 {self.cleaning.removed_fillers}、合并重复 {self.cleaning.merged_duplicates}"
            )
        return "；".join(parts)


def clean_lines(lines: list[SubtitleLine]) -> tuple[list[SubtitleLine], CleaningStats]:
    """保守清洗字幕行，返回（清洗结果, 统计）。

    规则：删除纯声音/氛围标记行；剔除行内标记符号；合并内容完全相同的
    连续重复行（AI 字幕的常见瑕疵）。其余文本一字不动。
    """
    stats = CleaningStats(total_in=len(lines))
    out: list[SubtitleLine] = []
    last_text: str | None = None
    for line in lines:
        text = " ".join(_INLINE_MARK.sub("", line.content).split())
        if not text or _FILLER_LINE.match(text) or not re.search(r"[\w\u4e00-\u9fff]", text):
            stats.removed_fillers += 1
            continue
        if text == last_text:
            stats.merged_duplicates += 1
            continue
        out.append(line.model_copy(update={"content": text}))
        last_text = text
    stats.total_out = len(out)
    return out, stats


def audit_markdown(markdown: str, cleaning: CleaningStats | None = None) -> AuditReport:
    """对渲染后的 Markdown 做排版体检（一级标题以外的空行分隔块视为段落）。"""
    paragraphs = [p for p in markdown.split("\n\n") if p.strip() and not p.startswith("# ")]
    lengths = [len(p.strip()) for p in paragraphs]
    report = AuditReport(
        paragraphs=len(lengths),
        max_paragraph_chars=max(lengths, default=0),
        avg_paragraph_chars=round(sum(lengths) / len(lengths)) if lengths else 0,
        long_paragraph_count=sum(1 for n in lengths if n > 500),
        fragment_count=sum(1 for n in lengths if n < 15),
        cleaning=cleaning,
    )
    return report


def format_report(report: AuditReport) -> str:
    """审查报告的多行文本（预览场景展示用）。"""
    lines = [
        "—— 审查报告 ——",
        f"分段排版：{report.one_line()}",
        f"平均段长：{report.avg_paragraph_chars} 字",
    ]
    if report.cleaning:
        lines.append(
            f"内容清洗：输入 {report.cleaning.total_in} 行 → 输出 {report.cleaning.total_out} 行"
            f"（删除无效 {report.cleaning.removed_fillers}、合并重复 {report.cleaning.merged_duplicates}）"
        )
    if report.long_paragraph_count == 0 and report.fragment_count == 0:
        lines.append("结论：排版良好，无需调整")
    else:
        if report.long_paragraph_count:
            lines.append("提示：存在超长段，多为说话无停顿的长篇内容，可接受或后续迭代优化分段规则")
        if report.fragment_count:
            lines.append("提示：存在碎段，多为说话停顿密集处")
    return "\n".join(lines)
