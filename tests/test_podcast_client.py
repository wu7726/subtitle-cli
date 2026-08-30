"""PodcastClient 单测：MockTransport 全离线覆盖输入解析与文稿提取。"""

from pathlib import Path

import httpx
import pytest

from subtitle_cli.podcast import feed as feed_mod
from subtitle_cli.podcast.client import PodcastClient, PodcastError, choose_transcript

FIXTURES = Path(__file__).parent / "fixtures"

EP3_HTML = "<html><body><p>第一段中文内容。</p><p>第二段中文内容。</p></body></html>"

VTT_TEXT = (FIXTURES / "podcast_transcript.vtt").read_text(encoding="utf-8")


def make_client(handler) -> PodcastClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return PodcastClient(http_client=http, sleep=lambda s: None, rng=__import__("random").Random(1))


def mock_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "itunes.apple.com/lookup" in url:
        return httpx.Response(200, json=__import__("json").loads(
            (FIXTURES / "itunes_lookup.json").read_text(encoding="utf-8")
        ))
    if url.startswith("https://example.com/feed.xml"):
        return httpx.Response(200, content=(FIXTURES / "podcast_feed.xml").read_bytes())
    if url == "https://example.com/transcripts/ep2.vtt":
        return httpx.Response(200, text=VTT_TEXT)
    if url == "https://example.com/transcripts/ep3.html":
        return httpx.Response(200, text=EP3_HTML, headers={"content-type": "text/html"})
    return httpx.Response(404, text="missing")


# ---- resolve_input ----
def test_resolve_apple_link_via_lookup():
    with make_client(mock_handler) as client:
        feed_url = client.resolve_input(
            "https://podcasts.apple.com/cn/podcast/示例播客/id123456?i=999"
        )
    assert feed_url == "https://example.com/feed.xml"


def test_resolve_plain_rss_url_passthrough():
    with make_client(mock_handler) as client:
        assert client.resolve_input("https://example.com/feed.xml") == "https://example.com/feed.xml"


def test_resolve_xiaoyuzhou_gives_guidance():
    with make_client(mock_handler) as client:
        with pytest.raises(ValueError) as exc:
            client.resolve_input("https://www.xiaoyuzhoufm.com/podcast/abc123")
    assert "RSS" in str(exc.value)


def test_resolve_garbage_raises_value_error():
    with make_client(mock_handler) as client:
        with pytest.raises(ValueError):
            client.resolve_input("随便一段话")


def test_resolve_apple_unknown_id_raises():
    def empty_lookup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"resultCount": 0, "results": []})

    with make_client(empty_lookup) as client:
        with pytest.raises(ValueError):
            client.resolve_input("https://podcasts.apple.com/podcast/x/id42")


# ---- list_episodes ----
def test_list_episodes_chronological_with_transcripts():
    with make_client(mock_handler) as client:
        title, episodes = client.list_episodes("https://example.com/feed.xml")
    assert title == "示例播客·离线调试"
    assert [ep.index for ep in episodes] == [1, 2, 3]
    assert episodes[0].title == "EP01 最早的一集"
    assert episodes[0].transcript_url is None
    assert episodes[1].transcript_url == "https://example.com/transcripts/ep2.vtt"
    # 单集页面链接进 source_url（属性头 source 来源）
    assert episodes[2].source_url == "https://example.com/episodes/3"
    assert client.uploader_name("whatever") == "主播小张"


def test_list_episodes_invalid_feed_raises_podcast_error():
    def html_feed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>not a feed</body></html>")

    with make_client(html_feed) as client:
        with pytest.raises(PodcastError):
            client.list_episodes("https://example.com/not-a-feed")


def test_list_episodes_http_error_raises_podcast_error():
    with make_client(mock_handler) as client:
        with pytest.raises(PodcastError):
            client.list_episodes("https://example.com/missing")


# ---- fetch_subtitles ----
def test_fetch_vtt_transcript():
    with make_client(mock_handler) as client:
        _, episodes = client.list_episodes("https://example.com/feed.xml")
        track = client.fetch_subtitles(episodes[1])
    assert track is not None
    assert track.lan == "zh"  # feed 声明的 lang 透传
    assert len(track.lines) == 3
    assert track.lines[0].content == "大家好，欢迎收听本期节目。"


def test_fetch_html_transcript_strips_tags():
    with make_client(mock_handler) as client:
        _, episodes = client.list_episodes("https://example.com/feed.xml")
        track = client.fetch_subtitles(episodes[2])
    assert track is not None
    assert [line.content for line in track.lines] == ["第一段中文内容。", "第二段中文内容。"]


def test_fetch_no_transcript_returns_none():
    with make_client(mock_handler) as client:
        _, episodes = client.list_episodes("https://example.com/feed.xml")
        assert client.fetch_subtitles(episodes[0]) is None


def test_fetch_transcript_404_raises_podcast_error():
    with make_client(mock_handler) as client:
        _, episodes = client.list_episodes("https://example.com/feed.xml")
        broken = episodes[1].model_copy(update={"transcript_url": "https://example.com/transcripts/404"})
        with pytest.raises(PodcastError):
            client.fetch_subtitles(broken)


# ---- 选轨 ----
def test_choose_transcript_prefers_chinese_then_format():
    refs = [
        feed_mod.TranscriptRef(url="en.srt", mime="application/srt", lang="en"),
        feed_mod.TranscriptRef(url="zh.html", mime="text/html", lang="zh"),
        feed_mod.TranscriptRef(url="zh.vtt", mime="text/vtt", lang="zh"),
    ]
    assert choose_transcript(refs).url == "zh.vtt"
    assert choose_transcript(refs[:2]).url == "zh.html"  # 中文优先于格式
    assert choose_transcript([]) is None
