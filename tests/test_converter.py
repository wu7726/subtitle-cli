"""converter 单测：段落规则/空白归一化/空字幕/确定性（技术方案 §7）。

规则参数来自 config：段长 ≥ 80 字符且行尾为句末标点闭合；起点间隔 > 2.0s 闭合。
"""

from subtitle_cli.bilibili.models import SubtitleLine
from subtitle_cli.converter import subtitle_to_markdown


def line(frm: float, to: float, content: str) -> SubtitleLine:
    return SubtitleLine(from_time=frm, to_time=to, content=content)


def test_empty_lines_yields_title_only():
    assert subtitle_to_markdown("标题", []) == "# 标题\n"


def test_title_is_h1_with_blank_line_after():
    out = subtitle_to_markdown("标题", [line(0, 1, "大家好。")])
    assert out == "# 标题\n\n大家好。\n"


def test_pause_gap_closes_paragraph():
    lines = [
        line(0, 1, "大家好，"),
        line(1, 2, "今天聊聊天。"),
        line(10, 11, "新话题开始。"),  # 与上一行起点差 9s > 2s
    ]
    out = subtitle_to_markdown("标题", lines)
    assert out == "# 标题\n\n大家好，今天聊聊天。\n\n新话题开始。\n"


def test_gap_of_exactly_threshold_does_not_close():
    lines = [line(0, 1, "第一句。"), line(2.0, 3, "第二句。")]
    out = subtitle_to_markdown("标题", lines)
    assert "第一句。第二句。\n" in out  # 仍在同一段
    assert out.count("\n\n") == 1  # 只有标题后一个空行


def test_long_paragraph_with_sentence_ending_closes():
    long_text = "字" * 79 + "。"
    lines = [
        line(0, 1, long_text),  # 80 字符且以。结尾 → 闭合
        line(1, 2, "第二段开始。"),
    ]
    out = subtitle_to_markdown("标题", lines)
    assert out == f"# 标题\n\n{long_text}\n\n第二段开始。\n"


def test_long_paragraph_without_sentence_ending_does_not_close():
    lines = [
        line(0, 1, "字" * 81),  # 足够长但没有句末标点
        line(1, 2, "继续接上。"),  # 间隔 1s，也不触发停顿
    ]
    out = subtitle_to_markdown("标题", lines)
    assert "字" * 81 + "继续接上。" in out


def test_short_line_with_sentence_ending_does_not_close():
    lines = [
        line(0, 1, "短句。"),
        line(1, 2, "还是同段。"),
    ]
    out = subtitle_to_markdown("标题", lines)
    assert "短句。还是同段。" in out


def test_whitespace_normalization():
    lines = [line(0, 1, "  大家   好 \t 世界  ")]
    out = subtitle_to_markdown("标题", lines)
    assert "大家 好 世界" in out


def test_blank_content_lines_skipped_and_do_not_affect_gap():
    lines = [
        line(0, 1, "第一段。"),
        line(1.5, 1.6, "   "),  # 空行应被跳过
        line(4, 5, "间隔超两秒后的新段。"),
    ]
    out = subtitle_to_markdown("标题", lines)
    assert out == "# 标题\n\n第一段。\n\n间隔超两秒后的新段。\n"


def test_deterministic_output():
    lines = [line(0, 1, "A。"), line(3, 4, "B。")]
    a = subtitle_to_markdown("标题", lines)
    b = subtitle_to_markdown("标题", lines)
    assert a == b


def test_fixture_conversion_structure(load_fixture):
    """用录制的字幕结构跑通整体转换，锁定输出形态。"""
    raw = load_fixture("subtitle.json")
    lines = [SubtitleLine.model_validate(item) for item in raw["body"]]
    out = subtitle_to_markdown("第1集 测试合集", lines)
    assert out.startswith("# 第1集 测试合集\n")
    paragraphs = [p for p in out.split("\n\n") if p]
    assert paragraphs[0] == "# 第1集 测试合集"
    assert len(paragraphs) > 3  # fixture 含多处 >2s 停顿
    assert all(p.strip() for p in paragraphs)
    assert out.endswith("\n")
    # 不应混入时间轴或 JSON 痕迹
    assert "from" not in out and "to_time" not in out
