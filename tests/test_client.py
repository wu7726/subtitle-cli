"""接口层单测：用 httpx.MockTransport 回放录制的 fixture，覆盖解析、签名、重试、限速注入。

全程无网络（技术方案 §9：单元测试不依赖网络）。
"""

from __future__ import annotations

import random
from typing import Callable

import httpx
import pytest

from subtitle_cli.bilibili.client import (
    BilibiliClient,
    BilibiliError,
    RiskControlError,
)
from subtitle_cli.bilibili.models import Episode

NOW = 1700000000

# 默认 nav 响应（未登录 -101，wbi_img 有效；密钥与社区文档示例一致）
DEFAULT_NAV = {
    "code": -101,
    "message": "账号未登录",
    "data": {
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        }
    },
}


class ClientHarness:
    """把 handler 接进 MockTransport，并记录请求与 sleep 便于断言。

    nav 请求默认由 DEFAULT_NAV 应答（签名密钥获取路径），除非显式传入 nav_payload。
    """

    def __init__(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        cookie: str | None = None,
        nav_payload: dict | None = DEFAULT_NAV,
    ):
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.nav_payload = nav_payload

        def tracked(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.nav_payload is not None and "nav" in request.url.path:
                return httpx.Response(200, json=self.nav_payload)
            return handler(request)

        self.client = BilibiliClient(
            cookie=cookie,
            http_client=httpx.Client(transport=httpx.MockTransport(tracked)),
            rng=random.Random(42),
            sleep=self.sleeps.append,
            now=lambda: NOW,
        )

    def __enter__(self) -> "ClientHarness":
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.close()

    def params_of(self, request: httpx.Request) -> dict:
        return dict(request.url.params)


@pytest.fixture
def episode() -> Episode:
    return Episode(bvid="BV17TtA6VEuH", cid=None, title="测试视频", index=1)


# ---- list_episodes ----
def test_list_episodes_single_page(load_fixture):
    payload = load_fixture("seasons_archives_list_p1.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.bilibili.com"
        return httpx.Response(200, json=payload)

    with ClientHarness(handler) as h:
        name, episodes = h.client.list_episodes("8016518")

    assert name == "合集·吃播"
    assert len(episodes) == 58
    assert episodes[0].bvid == "BV1QUVb6uEyn"
    assert episodes[0].index == 1 and episodes[-1].index == 58
    assert episodes[0].cid is None  # cid 需 pagelist 补齐
    # 请求参数符合接口约定
    sent = h.params_of(h.requests[0])
    assert sent["season_id"] == "8016518" and sent["page_num"] == "1" and sent["page_size"] == "100"
    assert len(h.requests) == 1  # 58 集 page_size=100 单页即全量


def test_list_episodes_pagination(load_fixture):
    """total > page_size 时逐页取全量：58 集、page_size=20 → 3 页。"""
    p1 = load_fixture("seasons_archives_list_p1_size20.json")
    p2 = load_fixture("seasons_archives_list_p2_size20.json")
    full = load_fixture("seasons_archives_list_p1.json")
    p3 = {
        "code": 0,
        "message": "0",
        "data": {
            "meta": full["data"]["meta"],
            "archives": full["data"]["archives"][40:58],  # 第 41~58 集
            "page": {"page_num": 3, "page_size": 20, "total": 58},
        },
    }
    pages = {1: p1, 2: p2, 3: p3}

    def handler(request: httpx.Request) -> httpx.Response:
        page_num = int(dict(request.url.params)["page_num"])
        return httpx.Response(200, json=pages[page_num])

    with ClientHarness(handler) as h:
        name, episodes = h.client.list_episodes("8016518")

    assert name == "合集·吃播"
    assert len(episodes) == 58
    assert [e.index for e in episodes] == list(range(1, 59))
    bvids = [e.bvid for e in episodes]
    assert len(set(bvids)) == 58  # 无重复、无遗漏
    assert len(h.requests) == 3


# ---- fetch_cid ----
def test_fetch_cid(load_fixture):
    payload = load_fixture("pagelist.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with ClientHarness(handler) as h:
        cid = h.client.fetch_cid("BV17TtA6VEuH")

    assert cid == "41362656971"


# ---- fetch_subtitles ----
def _player_with_subtitles(subtitles: list[dict]) -> dict:
    return {"code": 0, "message": "0", "data": {"subtitle": {"subtitles": subtitles}}}


def test_fetch_subtitles_no_cookie_returns_none(load_fixture, episode):
    """未登录响应（实测 fixture）：subtitles 恒为空 → None（无字幕）。"""
    payload = load_fixture("player_v2_nocookie.json")
    pagelist = load_fixture("pagelist.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if "pagelist" in request.url.path:
            return httpx.Response(200, json=pagelist)
        return httpx.Response(200, json=payload)

    with ClientHarness(handler) as h:
        assert h.client.fetch_subtitles(episode) is None
    assert episode.cid == "41362656971"  # cid 已自动补齐


def test_fetch_subtitles_prefers_zh_cn_then_downloads(load_fixture, episode):
    subtitle_json = load_fixture("subtitle.json")
    player = _player_with_subtitles(
        [
            {"lan": "ai-zh", "lan_doc": "AI中文", "subtitle_url": "//aisubtitle.hdslb.com/a.json"},
            {"lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "//aisubtitle.hdslb.com/b.json"},
        ]
    )
    pagelist = load_fixture("pagelist.json")
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "pagelist" in request.url.path:
            return httpx.Response(200, json=pagelist)
        if "wbi/v2" in request.url.path:
            return httpx.Response(200, json=player)
        seen_urls.append(str(request.url))
        return httpx.Response(200, json=subtitle_json)

    with ClientHarness(handler) as h:
        track = h.client.fetch_subtitles(episode)

    assert track is not None
    assert track.lan == "zh-CN"  # 人工 CC 优先于 AI 字幕
    assert len(track.lines) == len(subtitle_json["body"])
    assert track.lines[0].content == subtitle_json["body"][0]["content"]
    assert track.lines[0].from_time == pytest.approx(subtitle_json["body"][0]["from"])
    # 字幕 JSON 从补全协议的 hdslb 地址下载
    assert seen_urls[0].startswith("https://aisubtitle.hdslb.com/")


def test_fetch_subtitles_falls_back_to_first_track(load_fixture, episode):
    player = _player_with_subtitles(
        [{"lan": "en", "lan_doc": "英语", "subtitle_url": "https://aisubtitle.hdslb.com/en.json"}]
    )
    pagelist = load_fixture("pagelist.json")
    subtitle_json = load_fixture("subtitle.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if "pagelist" in request.url.path:
            return httpx.Response(200, json=pagelist)
        if "wbi/v2" in request.url.path:
            return httpx.Response(200, json=player)
        return httpx.Response(200, json=subtitle_json)

    with ClientHarness(handler) as h:
        track = h.client.fetch_subtitles(episode)
    assert track is not None and track.lan == "en"


def test_player_request_is_wbi_signed(load_fixture, episode):
    pagelist = load_fixture("pagelist.json")
    empty = load_fixture("player_v2_nocookie.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if "pagelist" in request.url.path:
            return httpx.Response(200, json=pagelist)
        return httpx.Response(200, json=empty)

    with ClientHarness(handler) as h:
        h.client.fetch_subtitles(episode)

    player_req = next(r for r in h.requests if "wbi/v2" in r.url.path)
    params = h.params_of(player_req)
    assert "w_rid" in params and "wts" in params  # 签名已附加
    assert params["bvid"] == "BV17TtA6VEuH"


def test_cookie_sent_to_api_but_not_to_subtitle_cdn(load_fixture, episode):
    pagelist = load_fixture("pagelist.json")
    player = _player_with_subtitles(
        [{"lan": "zh-CN", "lan_doc": "中文", "subtitle_url": "https://aisubtitle.hdslb.com/a.json"}]
    )
    subtitle_json = load_fixture("subtitle.json")
    cookie_seen: dict[str, bool] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cookie_seen[request.url.host] = "Cookie" in request.headers
        if "pagelist" in request.url.path:
            return httpx.Response(200, json=pagelist)
        if "wbi/v2" in request.url.path:
            return httpx.Response(200, json=player)
        return httpx.Response(200, json=subtitle_json)

    with ClientHarness(handler, cookie="SESSDATA=secret; buvid3=x") as h:
        h.client.fetch_subtitles(episode)

    assert cookie_seen["api.bilibili.com"] is True  # API 域透传 Cookie
    assert cookie_seen["aisubtitle.hdslb.com"] is False  # CDN 域不透传


# ---- 重试与风控 ----
def test_retry_on_http_412_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(412, text="risk")
        return httpx.Response(200, json={"code": 0, "data": {"archives": [], "page": {"total": 0}}})

    with ClientHarness(handler) as h:
        name, episodes = h.client.list_episodes("123")

    assert calls["n"] == 3  # 初始 1 次 + 重试 2 次
    assert episodes == []
    # 重试间隔为指数退避 + 抖动：至少有两次 > 0 的 sleep
    assert sum(1 for s in h.sleeps if s >= 2.0) >= 2


def test_risk_control_raises_after_retries_exhausted():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(412, text="risk")

    with ClientHarness(handler) as h:
        with pytest.raises(RiskControlError):
            h.client.list_episodes("123")

    assert calls["n"] == 4  # 1 次初始 + 最多 3 次重试


def test_retry_on_biz_risk_code():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"code": -352, "message": "风控"})
        return httpx.Response(200, json={"code": 0, "data": {"archives": [], "page": {"total": 0}}})

    with ClientHarness(handler) as h:
        _, episodes = h.client.list_episodes("123")
    assert calls["n"] == 2 and episodes == []


def test_no_retry_on_non_risk_biz_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"code": -400, "message": "请求错误"})

    with ClientHarness(handler) as h:
        with pytest.raises(BilibiliError) as excinfo:
            h.client.list_episodes("123")

    assert calls["n"] == 1  # 不可重试错误只请求一次
    assert "-400" in str(excinfo.value)


def test_retry_on_network_error_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"code": 0, "data": {"archives": [], "page": {"total": 0}}})

    with ClientHarness(handler) as h:
        _, episodes = h.client.list_episodes("123")
    assert calls["n"] == 2 and episodes == []


def test_network_error_raises_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with ClientHarness(handler) as h:
        with pytest.raises(BilibiliError, match="网络错误"):
            h.client.list_episodes("123")


def test_nav_unlogged_code_minus_101_tolerated():
    """nav 未登录返回 -101，但 wbi_img 有效：签名密钥获取应成功。"""
    with ClientHarness(lambda req: httpx.Response(500), nav_payload=DEFAULT_NAV) as h:
        img_key, sub_key = h.client._get_wbi_keys()
    assert img_key == "7cd084941338484aae1ad9425b84077c"
    assert sub_key == "4932caff0ff746eab6f01bf08b70ac45"


def test_wbi_keys_cached_across_requests(episode):
    pagelist_payload = {"code": 0, "data": [{"cid": 1, "page": 1, "part": "p"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if "pagelist" in request.url.path:
            return httpx.Response(200, json=pagelist_payload)
        return httpx.Response(200, json={"code": 0, "data": {"subtitle": {"subtitles": []}}})

    with ClientHarness(handler) as h:
        ep = episode.model_copy()
        h.client.fetch_subtitles(ep)
        ep2 = ep.model_copy(update={"cid": None})
        h.client.fetch_subtitles(ep2)

    nav_requests = [r for r in h.requests if "nav" in r.url.path]
    assert len(nav_requests) == 1  # 密钥进程内缓存，只取一次


# ---- BV 号 / 视频链接 → 所属合集 ----
def test_resolve_input_bvid_looks_up_season(load_fixture):
    """视频页 URL / 裸 BV 号 → view 接口查 ugc_season → season_id。"""
    view_payload = load_fixture("view_ugc_season.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "wbi/view" in request.url.path
        return httpx.Response(200, json=view_payload)

    with ClientHarness(handler) as h:
        season_id = h.client.resolve_input("https://www.bilibili.com/video/BV17TtA6VEuH/?p=1")
        assert season_id == "8016518"
        # 先 nav 取密钥，再发起已签名的 view 请求
        assert [("nav" in r.url.path, "wbi/view" in r.url.path) for r in h.requests] == [(True, False), (False, True)]
        assert "w_rid" in h.params_of(h.requests[1])


def test_resolve_input_bare_bvid(load_fixture):
    view_payload = load_fixture("view_ugc_season.json")

    with ClientHarness(lambda req: httpx.Response(200, json=view_payload)) as h:
        assert h.client.resolve_input("BV17TtA6VEuH") == "8016518"


def test_resolve_bvid_single_part_no_season_raises_value_error():
    """单P视频且不属于任何合集 → ValueError（没有可批量提取的内容）。"""
    payload = {"code": 0, "data": {"bvid": "BV1xx411c7mD", "videos": 1}}

    with ClientHarness(lambda req: httpx.Response(200, json=payload)) as h:
        with pytest.raises(ValueError, match="单P视频且不属于任何合集"):
            h.client.resolve_input("BV1xx411c7mD")


def test_resolve_and_list_multi_part_video():
    """多P视频（非合集体系）：resolve 返回 bvid，每个分P视作一集。"""
    view = {"code": 0, "data": {"bvid": "BV1DE0000001", "title": "示例多P课程", "videos": 3}}
    pagelist = {
        "code": 0,
        "data": [
            {"cid": 101, "page": 1, "part": "第一讲 认识C语言"},
            {"cid": 102, "page": 2, "part": "第二讲 编译器选择"},
            {"cid": 103, "page": 3, "part": ""},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "wbi/view" in request.url.path:
            return httpx.Response(200, json=view)
        if "pagelist" in request.url.path:
            return httpx.Response(200, json=pagelist)
        return httpx.Response(200, json={"code": 0, "data": {"subtitle": {"subtitles": []}}})

    with ClientHarness(handler) as h:
        ident = h.client.resolve_input("BV1DE0000001")
        assert ident == "BV1DE0000001"  # bvid 作为集合标识
        name, episodes = h.client.list_episodes(ident)
        assert name == "示例多P课程"
        assert [e.index for e in episodes] == [1, 2, 3]
        assert episodes[0].cid == "101" and episodes[0].title == "第一讲 认识C语言"
        assert episodes[2].title == "P3"  # 空 part 回退
        assert all(e.bvid == "BV1DE0000001" for e in episodes)
        # resolve 与 list_episodes 共享 view 缓存，只查询一次
        assert len([r for r in h.requests if "wbi/view" in r.url.path]) == 1


def test_resolve_bvid_video_not_found_wraps_error():
    """视频不存在（业务码 -404）→ 包装为带上下文的 BilibiliError。"""
    payload = {"code": -404, "message": "啥都木有"}

    with ClientHarness(lambda req: httpx.Response(200, json=payload)) as h:
        with pytest.raises(BilibiliError, match="查询视频 BV1xx411c7mD 信息失败"):
            h.client.resolve_input("BV1xx411c7mD")


def test_resolve_bvid_risk_control_stays_risk_error():
    """view 被风控时保持 RiskControlError 语义（不丢失给上层）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(412, text="risk")

    with ClientHarness(handler) as h:
        with pytest.raises(RiskControlError):
            h.client.resolve_input("BV1xx411c7mD")
    assert calls["n"] == 4  # 初始 + 3 次退避重试
