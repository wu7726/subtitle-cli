"""平台分派单测：输入识别与工厂创建，全部离线。"""

from subtitle_cli.dispatch import BILIBILI, PODCAST, create_client, detect_platform
from subtitle_cli.podcast.client import PodcastClient


def test_bilibili_inputs():
    assert detect_platform("https://www.bilibili.com/list/546195?sid=123") == BILIBILI
    assert detect_platform("https://b23.tv/abcd") == BILIBILI
    assert detect_platform("BV1abc123456") == BILIBILI
    assert detect_platform("123456") == BILIBILI
    assert detect_platform("") == BILIBILI  # 回落B站，报错信息保持旧版


def test_podcast_inputs():
    assert detect_platform("https://www.xiaoyuzhoufm.com/podcast/abc") == PODCAST
    assert detect_platform("https://podcasts.apple.com/cn/podcast/x/id123") == PODCAST
    assert detect_platform("https://example.com/feed.xml") == PODCAST
    assert detect_platform("http://feeds.example.com/rss") == PODCAST


def test_create_client_returns_podcast_for_rss():
    client = create_client("https://example.com/feed.xml")
    assert isinstance(client, PodcastClient)
    client.close()


def test_create_client_ignores_cookie_for_podcast():
    client = create_client("https://example.com/feed.xml", cookie="SESSDATA=x")
    assert isinstance(client, PodcastClient)
    client.close()
