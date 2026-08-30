"""DouyinClient 单测：输入解析、detail 解析、媒体下载，全部 MockTransport 离线。"""

from __future__ import annotations

import json
import random
from pathlib import Path

import httpx
import pytest

from subtitle_cli.bilibili.models import Episode
from subtitle_cli.douyin.client import (
    DouyinClient,
    DouyinError,
    clean_title,
    extract_aweme_id,
)

FIXTURES = Path(__file__).parent / "fixtures"
DETAIL = json.loads((FIXTURES / "douyin_detail.json").read_text(encoding="utf-8"))
VID = "7262554372352085267"


def make_client(handler) -> DouyinClient:
    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return DouyinClient(http_client=http, sleep=lambda s: None, rng=random.Random(1))


def mock_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.startswith("https://ttwid.bytedance.com/"):
        return httpx.Response(200, headers={"set-cookie": "ttwid=test-ttwid-123; Path=/"})
    if url.startswith("https://v.douyin.com/"):
        return httpx.Response(302, headers={"location": f"https://www.douyin.com/video/{VID}"})
    if url.startswith("https://www.douyin.com/aweme/v1/web/aweme/detail/"):
        return httpx.Response(200, json=DETAIL)
    if url.startswith("https://cdn.example.com/"):
        return httpx.Response(200, content=b"mp4-bytes")
    return httpx.Response(404, text="missing")


# ---- 输入解析 ----
def test_extract_aweme_id_variants():
    assert extract_aweme_id(f"https://www.douyin.com/video/{VID}") == VID
    assert extract_aweme_id(f"https://www.douyin.com/note/{VID}?x=1") == VID
    assert extract_aweme_id(f"https://www.iesdouyin.com/share/video/{VID}/") == VID
    assert extract_aweme_id(VID) == VID
    assert extract_aweme_id("12345") is None  # 太短：B站 season_id，不是抖音
    assert extract_aweme_id("随便") is None


def test_clean_title_strips_hashtags():
    assert clean_title("标题 #手机技巧 #教程") == "标题"
    assert clean_title("  多   空格  ") == "多 空格"


def test_resolve_input_variants():
    with make_client(mock_handler) as client:
        assert client.resolve_input(f"https://www.douyin.com/video/{VID}") == VID
        assert client.resolve_input(VID) == VID
        # 分享口令：文字 + 短链，302 落到视频页
        assert (
            client.resolve_input(f"7.42 复制打开抖音 https://v.douyin.com/iAbCdEf/ 看视频")
            == VID
        )


def test_resolve_input_garbage_raises():
    with make_client(mock_handler) as client:
        with pytest.raises(ValueError):
            client.resolve_input("随便一段话没有链接")


def test_resolve_short_link_unknown_target_raises():
    def dead_short(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://v.douyin.com/"):
            return httpx.Response(302, headers={"location": "https://www.douyin.com/"})
        return httpx.Response(200, text="<html>homepage</html>")

    with make_client(dead_short) as client:
        with pytest.raises(ValueError):
            client.resolve_input("https://v.douyin.com/xxxxx/")


# ---- list_episodes ----
def test_list_episodes_maps_detail_fields():
    with make_client(mock_handler) as client:
        collection, episodes = client.list_episodes(VID)
    assert collection == "测试作者"
    assert client.uploader_name(VID) == "测试作者"
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep.index == 1
    assert ep.title == "如何把几段视频连接起来 合成完整视频"  # 话题标签已剥离
    assert ep.source_url == f"https://www.douyin.com/video/{VID}"
    assert ep.audio_url == "https://cdn.example.com/low.mp4"  # 最低码率档：音轨相同体积小
    assert ep.published == "2023-08-02"


def test_fetch_subtitles_always_none():
    with make_client(mock_handler) as client:
        _, episodes = client.list_episodes(VID)
        assert client.fetch_subtitles(episodes[0]) is None


# ---- 媒体下载 ----
def test_download_audio_writes_file(tmp_path):
    with make_client(mock_handler) as client:
        _, episodes = client.list_episodes(VID)
        dest = tmp_path / "media.bin"
        client.download_audio(episodes[0], dest)
    assert dest.read_bytes() == b"mp4-bytes"


def test_download_audio_without_media_raises(tmp_path):
    with make_client(mock_handler) as client:
        with pytest.raises(DouyinError):
            client.download_audio(
                Episode(bvid=VID, title="t", index=1),
                tmp_path / "a.bin",
            )


# ---- 异常路径 ----
def test_detail_blocked_raises_risk_hint():
    def blocked(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://ttwid.bytedance.com/"):
            return httpx.Response(200, headers={"set-cookie": "ttwid=test-ttwid-123; Path=/"})
        return httpx.Response(403, text="blocked")

    with make_client(blocked) as client:
        with pytest.raises(DouyinError) as exc:
            client.list_episodes(VID)
    assert "风控" in str(exc.value)


def test_detail_missing_video_raises():
    def empty(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://ttwid.bytedance.com/"):
            return httpx.Response(200, headers={"set-cookie": "ttwid=test-ttwid-123; Path=/"})
        return httpx.Response(200, json={"status_code": 0, "aweme_detail": None})

    with make_client(empty) as client:
        with pytest.raises(DouyinError) as exc:
            client.list_episodes(VID)
    assert "不存在" in str(exc.value)


def test_ttwid_failure_raises():
    def no_cookie(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    with make_client(no_cookie) as client:
        with pytest.raises(DouyinError) as exc:
            client.list_episodes(VID)
    assert "ttwid" in str(exc.value)
