"""平台分派单测：输入识别与工厂创建，全部离线。"""

from subtitle_cli.dispatch import BILIBILI, DOUYIN, PODCAST, create_client, detect_platform
from subtitle_cli.douyin.client import DouyinClient
from subtitle_cli.podcast.client import PodcastClient


def test_bilibili_inputs():
    assert detect_platform("https://www.bilibili.com/list/546195?sid=123") == BILIBILI
    assert detect_platform("https://b23.tv/abcd") == BILIBILI
    assert detect_platform("BV1abc123456") == BILIBILI
    assert detect_platform("123456") == BILIBILI
    assert detect_platform("") == BILIBILI  # 回落B站，报错信息保持旧版


def test_douyin_inputs():
    assert detect_platform("https://www.douyin.com/video/7262554372352085267") == DOUYIN
    assert detect_platform("https://www.douyin.com/note/7262554372352085267") == DOUYIN
    assert detect_platform("https://www.iesdouyin.com/share/video/7262554372352085267/") == DOUYIN
    assert detect_platform("https://v.douyin.com/iAbCdEf/") == DOUYIN
    assert detect_platform("7262554372352085267") == DOUYIN  # 15-20 位裸 ID
    assert detect_platform("8016518") == BILIBILI  # 短数字仍是B站 season_id


def test_podcast_inputs():
    assert detect_platform("https://www.xiaoyuzhoufm.com/podcast/abc") == PODCAST
    assert detect_platform("https://podcasts.apple.com/cn/podcast/x/id123") == PODCAST
    assert detect_platform("https://example.com/feed.xml") == PODCAST
    assert detect_platform("http://feeds.example.com/rss") == PODCAST


def test_create_client_returns_podcast_for_rss():
    client = create_client("https://example.com/feed.xml")
    assert isinstance(client, PodcastClient)
    client.close()


def test_create_client_returns_douyin_for_douyin_input():
    client = create_client("https://www.douyin.com/video/7262554372352085267")
    assert isinstance(client, DouyinClient)
    client.close()


def test_create_client_ignores_cookie_for_non_bilibili():
    client = create_client("https://example.com/feed.xml", cookie="SESSDATA=x")
    assert isinstance(client, PodcastClient)
    client.close()


def test_platform_subdir_read_and_write():
    from subtitle_cli.vault import VaultConfig

    from subtitle_cli.dispatch import platform_subdir, set_platform_subdir

    cfg = VaultConfig()
    assert platform_subdir(cfg, BILIBILI) == "B站字幕"
    assert platform_subdir(cfg, PODCAST) == "播客字幕"
    assert platform_subdir(cfg, DOUYIN) == "抖音字幕"

    set_platform_subdir(cfg, PODCAST, "学习/播客")
    set_platform_subdir(cfg, DOUYIN, "抖音存档")
    set_platform_subdir(cfg, BILIBILI, "学习/B站字幕")
    assert cfg.podcast_subdir == "学习/播客"
    assert cfg.douyin_subdir == "抖音存档"
    assert cfg.subdir == "学习/B站字幕"
