"""RSS feed 解析单测：命名空间、字段提取、时间排序，全部离线。"""

from pathlib import Path

import pytest

from subtitle_cli.podcast import feed as feed_mod
from subtitle_cli.podcast.feed import FeedError, parse_feed, sort_items_chronological

FIXTURES = Path(__file__).parent / "fixtures"


def _parse_fixture() -> feed_mod.ParsedFeed:
    return parse_feed((FIXTURES / "podcast_feed.xml").read_bytes())


def test_channel_title_and_author():
    parsed = _parse_fixture()
    assert parsed.title == "示例播客·离线调试"
    assert parsed.author == "主播小张"
    assert len(parsed.items) == 3


def test_item_fields_extracted():
    parsed = _parse_fixture()
    newest = parsed.items[0]  # feed 原始顺序：最新在前
    assert newest.title == "EP03 最新的话题"
    assert newest.guid == "https://example.com/episodes/3"
    assert newest.audio_url == "https://example.com/audio/ep3.mp3"
    assert newest.pub_date is not None
    assert len(newest.transcripts) == 2
    langs = {t.lang for t in newest.transcripts}
    assert langs == {"en", "zh"}


def test_item_without_transcript_has_empty_list():
    parsed = _parse_fixture()
    assert parsed.items[2].transcripts == []


def test_chronological_sort_puts_oldest_first_undated_last():
    parsed = _parse_fixture()
    ordered = sort_items_chronological(parsed.items)
    assert [item.title for item in ordered] == [
        "EP01 最早的一集",
        "EP02 中间的一集",
        "EP03 最新的话题",
    ]
    # 无日期条目排在有日期之后，保持原相对顺序
    no_date = feed_mod.FeedItem(title="无日期")
    ordered2 = sort_items_chronological([no_date, parsed.items[0]])
    assert [item.title for item in ordered2] == ["EP03 最新的话题", "无日期"]


def test_invalid_xml_raises_feed_error():
    with pytest.raises(FeedError):
        parse_feed(b"<html><body>not a feed</body></html>")


def test_html_without_channel_raises_feed_error():
    with pytest.raises(FeedError):
        parse_feed(b"<root><nothing/></root>")
