"""播客平台客户端：PlatformClient 协议的播客实现。

数据源 = 播客 RSS。支持三种输入：RSS 地址（通用）、Apple Podcasts 节目链接
（iTunes lookup 反查 RSS）、小宇宙链接（网页端拿不到 RSS，给出 App 内复制
RSS 的指引）。文稿来自 RSS 的 podcast:transcript 标签（Podcasting 2.0）；
没有文稿的单集返回 None（走「无字幕」分支），不下载音频、不做语音转写。
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from collections.abc import Callable

import httpx

from .. import config
from ..bilibili.models import Episode, SubtitleTrack
from ..errors import PlatformError
from . import feed as feed_mod
from . import transcripts

ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"

XIAOYUZHU_GUIDANCE = (
    "小宇宙的网页端不公开节目的 RSS 地址，无法直接从节目/单集链接提取。"
    "请在小宇宙 App 中打开该节目 → 右上角「…」→「复制 RSS 链接」，"
    "把得到的 RSS 地址粘贴到这里即可。"
)


class PodcastError(PlatformError):
    """播客接口调用失败（网络、RSS 无效、文稿下载失败）。"""


def choose_transcript(refs: list[feed_mod.TranscriptRef]) -> feed_mod.TranscriptRef | None:
    """选轨：中文优先；同语言下格式越标准越好（HTML 最后）。稳定排序取首个。"""
    if not refs:
        return None
    # text/html 需剥标签兜底，排最后；未知 MIME 靠内容嗅探，介于两者之间
    mime_rank = {"text/vtt": 0, "application/vtt": 0, "application/srt": 0, "text/srt": 0,
                 "application/json": 1, "text/plain": 1}
    return sorted(
        refs,
        key=lambda ref: (
            0 if ref.lang.lower().startswith("zh") else 1,
            mime_rank.get(ref.mime.lower(), 2 if "html" in ref.mime.lower() else 1),
        ),
    )[0]


class PodcastClient:
    """PlatformClient 协议的播客实现。全程串行，请求间随机间隔。"""

    platform = "podcast"  # pipeline 据此决定索引页形态
    base_tag = "播客字幕"  # 笔记基础标签（B站为「B站字幕」）

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._http = http_client or httpx.Client(
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        self._rng = rng or random.Random()
        self._sleep = sleep or time.sleep
        self._author = ""
        self._transcript_lang: dict[str, str] = {}  # 文稿 URL → feed 声明的语言

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PodcastClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- PlatformClient 协议 ----
    def resolve_input(self, raw: str) -> str:
        """小宇宙链接给出指引；Apple 链接反查 RSS；其余 http(s) 视为 RSS 地址。"""
        text = (raw or "").strip()
        if "xiaoyuzhoufm.com" in text:
            raise ValueError(XIAOYUZHU_GUIDANCE)
        match = re.search(r"podcasts\.apple\.com/[^\s]*?id(\d+)", text)
        if match:
            return self._resolve_apple_id(match.group(1))
        if re.match(r"^https?://", text):
            return text
        raise ValueError(
            f"无法从输入中识别播客来源：{text[:80]!r}。"
            f"支持播客 RSS 地址、Apple Podcasts 节目链接；B站合集请直接粘贴"
            f"合集页或视频链接。"
        )

    def list_episodes(self, feed_url: str) -> tuple[str, list[Episode]]:
        """取节目全部单集，按发布时间升序编号（最早 = 第 1 集）。"""
        parsed = self._fetch_feed(feed_url)
        self._author = parsed.author
        episodes: list[Episode] = []
        seen: set[str] = set()
        for item in feed_mod.sort_items_chronological(parsed.items):
            key = item.guid or item.link or item.audio_url
            if key and key in seen:
                continue
            seen.add(key)
            index = len(episodes) + 1
            chosen = choose_transcript(item.transcripts)
            if chosen:
                self._transcript_lang[chosen.url] = chosen.lang
            episodes.append(
                Episode(
                    bvid=key or f"podcast-{index}",
                    title=item.title or f"第{index}集",
                    index=index,
                    source_url=item.link if item.link.startswith("http") else (item.guid if item.guid.startswith("http") else ""),
                    transcript_url=chosen.url if chosen else None,
                    audio_url=item.audio_url,
                )
            )
        title = parsed.title or feed_url
        return title, episodes

    def fetch_subtitles(self, episode: Episode) -> SubtitleTrack | None:
        """下载该集现成文稿并解析为字幕轨；无文稿或文稿为空返回 None。"""
        if not episode.transcript_url:
            return None
        url = episode.transcript_url
        content, mime = self._get_text(url, delay_range=config.MEDIA_DELAY_RANGE)
        lines = transcripts.parse_transcript(content, mime)
        if not lines:
            return None
        return SubtitleTrack(lan=self._transcript_lang.get(url) or "zh", lines=lines)

    def uploader_name(self, bvid: str) -> str:
        """节目作者（RSS itunes:author；失败返回空串，不阻断主流程）。"""
        return self._author

    def download_audio(self, episode: Episode, dest: Path) -> Path:
        """下载单集音频（RSS enclosure）到 dest（ASR 兜底用；协议可选能力）。"""
        if not episode.audio_url:
            raise PodcastError(f"该单集没有音频附件（enclosure）：{episode.title}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._http.stream(
                "GET", episode.audio_url, timeout=config.REQUEST_TIMEOUT * 4
            ) as resp:
                if resp.status_code != 200:
                    raise PodcastError(
                        f"音频下载失败（HTTP {resp.status_code}）：{episode.audio_url}"
                    )
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        except httpx.TransportError as exc:
            raise PodcastError(
                f"音频下载网络错误（{exc.__class__.__name__}）：{episode.audio_url}"
            ) from exc
        return dest

    # ---- 内部 ----
    def _resolve_apple_id(self, apple_id: str) -> str:
        try:
            payload = self._get_json(
                ITUNES_LOOKUP_URL,
                params={"id": apple_id},
                delay_range=config.LIST_DELAY_RANGE,
            )
        except PodcastError as exc:
            raise ValueError(f"通过 Apple Podcasts 反查 RSS 失败：{exc}") from exc
        for result in payload.get("results", []):
            feed_url = result.get("feedUrl")
            if feed_url:
                return str(feed_url)
        raise ValueError(f"Apple Podcasts 上找不到 id={apple_id} 对应节目的 RSS 源。")

    def _fetch_feed(self, feed_url: str) -> feed_mod.ParsedFeed:
        content, _ = self._get_text(feed_url, delay_range=config.LIST_DELAY_RANGE)
        try:
            return feed_mod.parse_feed(content.encode("utf-8"))
        except feed_mod.FeedError as exc:
            raise PodcastError(
                f"该链接不是有效的播客 RSS 源：{exc}。"
                f"请确认粘贴的是节目 RSS 地址（可在播客 App 中复制）。"
            ) from exc

    def _get_text(self, url: str, *, delay_range: tuple[float, float]) -> tuple[str, str]:
        """GET 文本资源，返回（正文, content-type）；一次网络重试。"""
        resp = self._request(url, delay_range=delay_range)
        return resp.text, resp.headers.get("content-type", "")

    def _get_json(self, url: str, *, params: dict, delay_range: tuple[float, float]) -> dict:
        resp = self._request(url, params=params, delay_range=delay_range)
        try:
            return resp.json()
        except ValueError as exc:
            raise PodcastError(f"响应不是合法 JSON：{url}") from exc

    def _request(
        self, url: str, *, params: dict | None = None, delay_range: tuple[float, float]
    ) -> httpx.Response:
        last_error: Exception | None = None
        for _attempt in range(2):
            self._sleep(self._rng.uniform(*delay_range))
            try:
                resp = self._http.get(url, params=params)
            except httpx.TransportError as exc:
                last_error = exc
                continue
            if resp.status_code != 200:
                raise PodcastError(f"HTTP {resp.status_code}：{url}")
            return resp
        raise PodcastError(f"网络错误（{last_error.__class__.__name__}）：{url}") from last_error
