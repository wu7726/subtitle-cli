"""文稿解析器单测：VTT / SRT / JSON / 纯文本 / 嗅探，全部离线。"""

from pathlib import Path

import pytest

from subtitle_cli.podcast import transcripts
from subtitle_cli.podcast.transcripts import parse_timestamp

FIXTURES = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---- 时间戳 ----
def test_parse_timestamp_variants():
    assert parse_timestamp("00:00:01.500") == 1.5
    assert parse_timestamp("01:02:03,400") == 3723.4
    assert parse_timestamp("02:03.5") == 123.5
    assert parse_timestamp("07.25") == 7.25


# ---- VTT ----
def test_parse_vtt_fixture():
    lines = transcripts.parse_vtt(_text("podcast_transcript.vtt"))
    assert len(lines) == 3  # NOTE 块被忽略；rolling 重复行保留（清洗阶段合并）
    assert lines[0].from_time == 0.0 and lines[0].to_time == 2.0
    assert lines[0].content == "大家好，欢迎收听本期节目。"
    assert lines[2].from_time == 8.0
    assert "播客字幕" in lines[2].content


def test_parse_vtt_empty_between_cues_kept_out():
    lines = transcripts.parse_vtt("WEBVTT\n\n00:00.000 --> 00:01.000\nonly line\n")
    assert [line.content for line in lines] == ["only line"]


# ---- SRT ----
def test_parse_srt_fixture():
    lines = transcripts.parse_srt(_text("podcast_transcript.srt"))
    assert [(line.from_time, line.to_time) for line in lines] == [(1.0, 3.0), (3.5, 6.0)]
    assert lines[1].content == "Today we talk about transcripts."


# ---- JSON ----
def test_parse_json_segments_fixture():
    lines = transcripts.parse_json_transcript(_text("podcast_transcript.json"))
    assert [(line.from_time, line.to_time, line.content) for line in lines] == [
        (0.0, 2.0, "第一句测试内容。"),
        (3.5, 5.0, "第二句测试内容。"),
    ]


def test_parse_json_bilibili_body_shape():
    payload = '{"body": [{"from": 0, "to": 1, "content": "内容。"}]}'
    lines = transcripts.parse_json_transcript(payload)
    assert lines[0].content == "内容。"


def test_parse_json_invalid_returns_empty():
    assert transcripts.parse_json_transcript("not json") == []


# ---- 纯文本与 HTML ----
def test_parse_plain_uniform_fake_timeline():
    lines = transcripts.parse_plain("第一行\n\n第二行")
    assert [(line.from_time, line.content) for line in lines] == [(0.0, "第一行"), (2.0, "第二行")]


def test_parse_transcript_html_strips_tags():
    html = "<div><p>第一段。</p><p>第二段。</p></div>"
    lines = transcripts.parse_transcript(html, "text/html")
    assert [line.content for line in lines] == ["第一段。", "第二段。"]


# ---- MIME 分派与嗅探 ----
def test_dispatch_by_mime_and_sniffing():
    vtt = _text("podcast_transcript.vtt")
    srt = _text("podcast_transcript.srt")
    assert len(transcripts.parse_transcript(vtt, "text/vtt")) == 3
    assert len(transcripts.parse_transcript(srt, "application/srt")) == 2
    # 无 MIME 时按内容嗅探
    assert len(transcripts.parse_transcript(vtt, "")) == 3
    assert len(transcripts.parse_transcript(srt, "unknown/type")) == 2


def test_sniff_json_and_plain():
    assert len(transcripts.parse_transcript(_text("podcast_transcript.json"), "")) == 2
    lines = transcripts.parse_transcript("一行\n二行", "weird")
    assert [line.content for line in lines] == ["一行", "二行"]


@pytest.mark.parametrize("mime", ["text/vtt", "application/srt", "application/json", "text/plain"])
def test_dispatch_never_raises_on_garbage(mime):
    assert transcripts.parse_transcript("", mime) == []
