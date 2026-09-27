"""抖音平台客户端：PlatformClient 协议的抖音实现（单视频）。

数据流：resolve_input 提取视频 ID → detail 接口取标题/作者/播放地址 →
fetch_subtitles 恒为 None（抖音无可直接抓取的字幕，正文一律走本地
语音转写兜底）→ download_audio 下载 CDN 直链 mp4 供 ASR 解码。
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections.abc import Callable

import httpx

from .. import config
from ..bilibili.models import Episode
from ..errors import PlatformError

API_DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"

DETACHED_HASHTAG = re.compile(r"#\S+")

DETAIL_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "170400",
    "version_name": "17.4.0",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "126.0.0.0",
}


class DouyinError(PlatformError):
    """抖音接口调用失败（网络、风控、视频不存在）。"""


def extract_aweme_id(raw: str) -> str | None:
    """从链接或纯数字输入中提取视频 ID；无法识别返回 None。"""
    text = (raw or "").strip()
    if re.fullmatch(r"\d{15,20}", text):
        return text
    return next((m for m in re.findall(r"/(?:video|note)/(\d{15,20})", text)), None)


def clean_title(desc: str) -> str:
    """去掉话题标签并归一空白，得到可入文件名/标题的文本。"""
    return " ".join(DETACHED_HASHTAG.sub("", desc or "").split())


class DouyinClient:
    """PlatformClient 协议的抖音实现。全程串行，请求间随机间隔。"""

    platform = "douyin"
    base_tag = "抖音字幕"

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._http = http_client or httpx.Client(
            headers={
                "User-Agent": config.USER_AGENT,
                "Referer": "https://www.douyin.com/",
            },
            timeout=config.REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        self._rng = rng or random.Random()
        self._sleep = sleep or time.sleep
        self._ttwid: str = ""
        self._item: dict | None = None  # 最近一次 detail 响应（作者名等复用）

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DouyinClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- PlatformClient 协议 ----
    def resolve_input(self, raw: str) -> str:
        """短链（自动跟随 302）、视频/图文页链接或裸数字 ID → 视频视频 ID。"""
        text = (raw or "").strip()
        # 分享口令里通常混着文字与短链，先抠出链接
        short = re.search(r"(?:https?://)?v\.douyin\.com/([\w-]+)", text)
        if short:
            return self._resolve_short_link(f"https://v.douyin.com/{short.group(1)}")
        aweme_id = extract_aweme_id(text)
        if aweme_id:
            return aweme_id
        raise ValueError(
            f"无法从输入中识别抖音视频：{text[:80]!r}。支持抖音 App「复制链接」得到的"
            f"分享口令（含 v.douyin.com 短链）、www.douyin.com/video/… 或 /note/… 页面链接。"
            f"抖音暂只支持单个视频，合集/主页批量尚未实现。"
        )

    def list_episodes(self, aweme_id: str) -> tuple[str, list[Episode]]:
        """单视频视作单集合集：合集名 = 作者昵称（笔记按作者归档）。"""
        item = self._fetch_detail(aweme_id)
        author = (item.get("author") or {}).get("nickname") or ""
        title = clean_title(item.get("desc") or "") or f"抖音视频{aweme_id}"
        episode = Episode(
            bvid=aweme_id,
            title=title,
            index=1,
            source_url=f"https://www.douyin.com/video/{aweme_id}",
            audio_url=self._pick_play_url(item),
            published=self._published_of(item),
        )
        return author or "抖音视频", [episode]

    def fetch_subtitles(self, episode: Episode) -> None:
        """抖音没有可直接抓取的字幕轨，恒返回 None（正文走 ASR 兜底）。"""
        return None

    def uploader_name(self, bvid: str) -> str:
        """作者昵称（来自最近一次 detail 响应）。"""
        return ((self._item or {}).get("author") or {}).get("nickname") or ""

    def download_audio(self, episode: Episode, dest: Path) -> Path:
        """下载视频直链（mp4 音视频混流，ASR 经 PyAV 解码音轨）到 dest。"""
        if not episode.audio_url:
            raise DouyinError(f"该视频没有可下载的媒体地址：{episode.title}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._http.stream("GET", episode.audio_url, timeout=config.REQUEST_TIMEOUT * 4) as resp:
                if resp.status_code not in (200, 206):
                    raise DouyinError(f"媒体下载失败（HTTP {resp.status_code}）：{episode.title}")
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        except httpx.TransportError as exc:
            raise DouyinError(f"媒体下载网络错误（{exc.__class__.__name__}）：{episode.title}") from exc
        return dest

    # ---- 内部 ----
    def _resolve_short_link(self, short_url: str) -> str:
        try:
            resp = self._http.get(short_url)
        except httpx.HTTPError as exc:
            raise DouyinError(f"短链解析网络错误（{exc.__class__.__name__}）") from exc
        final = str(resp.url)
        aweme_id = extract_aweme_id(final) or extract_aweme_id(resp.text[:5000])
        if not aweme_id:
            raise ValueError(f"短链没有指向可识别的视频页面：{short_url}")
        return aweme_id

    def _fetch_detail(self, aweme_id: str) -> dict:
        self._ensure_ttwid()
        self._sleep(self._rng.uniform(*config.LIST_DELAY_RANGE))
        try:
            resp = self._http.get(
                API_DETAIL_URL,
                params={"aweme_id": aweme_id, "msToken": "x" * 116, **DETAIL_PARAMS},
                cookies={"ttwid": self._ttwid, "msToken": "x" * 116},
            )
        except httpx.TransportError as exc:
            raise DouyinError(f"网络错误（{exc.__class__.__name__}）：{aweme_id}") from exc
        if resp.status_code == 403 or "blocked" in resp.text[:20]:
            raise DouyinError(
                f"抖音接口拒绝访问（HTTP {resp.status_code}，疑似风控）。"
                f"稍后重试；若持续失败说明接口策略变化，需要更新工具。"
            )
        if resp.status_code != 200:
            raise DouyinError(f"HTTP {resp.status_code}：{aweme_id}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise DouyinError(f"响应不是合法 JSON：{aweme_id}") from exc
        item = payload.get("aweme_detail")
        if not isinstance(item, dict) or not item:
            raise DouyinError(f"视频不存在或已被删除：{aweme_id}")
        self._item = item
        return item

    def _ensure_ttwid(self) -> None:
        if self._ttwid:
            return
        try:
            resp = self._http.post(
                TTWID_REGISTER_URL,
                json={
                    "region": "cn",
                    "aid": 1768,
                    "needFid": False,
                    "service": "www.ixigua.com",
                    "migrate_info": {"ticket": "", "source": "node"},
                    "cbUrlProtocol": "https",
                    "union": True,
                },
                headers={"User-Agent": config.USER_AGENT},
            )
        except httpx.TransportError as exc:
            raise DouyinError(f"ttwid 注册网络错误（{exc.__class__.__name__}）") from exc
        ttwid = resp.cookies.get("ttwid", "")
        if not ttwid:
            match = re.search(r"ttwid=([^;\s]+)", resp.headers.get("set-cookie", ""))
            ttwid = match.group(1) if match else ""
        if not ttwid:
            raise DouyinError("无法获取 ttwid（抖音前置 Cookie），接口调用会被拒绝。")
        self._ttwid = ttwid

    def _pick_play_url(self, item: dict) -> str:
        """选媒体直链：优先最低码率档（音轨相同、体积小），无档位回落 play_addr。"""
        gears = [
            g for g in (item.get("video") or {}).get("bit_rate") or []
            if ((g.get("play_addr") or {}).get("url_list") or [])
        ]
        gears.sort(key=lambda g: g.get("bit_rate") or 0)
        play = (gears[0]["play_addr"] if gears else (item.get("video") or {}).get("play_addr")) or {}
        urls = play.get("url_list") or []
        url = urls[0] if urls else ""
        # 无水印路径兜底：playwm → play（CDN 直链通常已是无水印形态）
        return url.replace("/playwm/", "/play/") if url else ""

    @staticmethod
    def _published_of(item: dict) -> str:
        create_time = item.get("create_time")
        if not isinstance(create_time, (int, float)):
            return ""
        return datetime.fromtimestamp(create_time, tz=timezone(timedelta(hours=8))).date().isoformat()
