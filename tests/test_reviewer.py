"""reviewer 单测：清洗规则（无效标记行、连续重复）与排版审查报告（纯函数）。"""

from subtitle_cli.bilibili.models import SubtitleLine
from subtitle_cli.converter import subtitle_to_markdown
from subtitle_cli.reviewer import audit_markdown, clean_lines, format_report


def line(content: str, frm: float = 0.0) -> SubtitleLine:
    return SubtitleLine(from_time=frm, to_time=frm + 1, content=content)


# ---- 清洗 ----
def test_clean_removes_filler_lines():
    lines = [
        line("大家好，今天讲第一章。"),
        line("（音乐）"),
        line("[掌声]"),
        line("♪"),
        line("接下来看代码。"),
    ]
    cleaned, stats = clean_lines(lines)
    assert [l.content for l in cleaned] == ["大家好，今天讲第一章。", "接下来看代码。"]
    assert stats.removed_fillers == 3
    assert stats.total_out == 2


def test_clean_strips_inline_markers_but_keeps_text():
    cleaned, _ = clean_lines([line("（音乐）起风了（音乐）这种感觉")])
    assert cleaned[0].content == "起风了这种感觉"


def test_clean_merges_consecutive_duplicates():
    lines = [line("对的对的"), line("对的对的"), line("对的对的"), line("下一句")]
    cleaned, stats = clean_lines(lines)
    assert [l.content for l in cleaned] == ["对的对的", "下一句"]
    assert stats.merged_duplicates == 2


def test_clean_preserves_normal_text_and_time():
    lines = [line("  前后有空格  ", frm=3.5)]
    cleaned, stats = clean_lines(lines)
    assert stats.removed_fillers == 0
    assert cleaned[0].content == "前后有空格"
    assert cleaned[0].from_time == 3.5


# ---- 审查 ----
def test_audit_counts_paragraph_stats():
    md = subtitle_to_markdown(
        "标题",
        [line("第一段内容。", 0), line("第二段内容比较长一些一些一些一些。", 10)],
    )
    report = audit_markdown(md)
    assert report.paragraphs == 2
    assert report.max_paragraph_chars >= 10
    assert report.long_paragraph_count == 0


def test_audit_flags_long_and_fragment_paragraphs():
    md = "# 标题\n\n" + "长" * 501 + "\n\n" + "短" * 5 + "\n"
    report = audit_markdown(md)
    assert report.long_paragraph_count == 1
    assert report.fragment_count == 1
    assert "超长段" in format_report(report) or "碎段" in format_report(report)


def test_format_report_clean_summary():
    _, stats = clean_lines([line("（音乐）"), line("正文。")])
    md = subtitle_to_markdown("标题", [line("正文。")])
    report = audit_markdown(md, stats)
    text = format_report(report)
    assert "内容清洗" in text and "删除无效 1" in text
