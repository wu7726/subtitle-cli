"""B站接口客户端：wbi 签名、限速、重试、风控识别。

本模块是项目中唯一接触网络的地方（技术方案 §2）。
限速与重试策略见 config.py 与技术方案 §5.4。
"""

from __future__ import annotations

import random
import re
import time
from typing import Callable

import httpx

from .. import config
from .models import (
    Episode,
    PageInfo,
    PlayerData,
    SeasonArchivesPage,
    SubtitleLine,
    SubtitleTrack,
    ViewData,
)
from .wbi import sign

API_BASE = "https://api.bilibili.com"
SEASON_ARCHIVES_URL = f"{API_BASE}/x/polymer/web-space/seasons_archives_list"
PAGELIST_URL = f"{API_BASE}/x/player/pagelist"
PLAYER_V2_URL = f"{API_BASE}/x/player/wbi/v2"
NAV_URL = f"{API_BASE}/x/web-interface/nav"
VIEW_URL = f"{API_BASE}/x/web-interface/wbi/view"


class BilibiliError(Exception):
    """接口调用失败（不可重试的业务/解析错误，或重试耗尽后的网络错误）。"""


class RiskControlError(BilibiliError):
    """风控信号（HTTP 412/429、业务码 -352/-412），重试耗尽后抛出。

    pipeline 据此统计连续风控次数并在达到阈值时提前终止。
    """


def extract_bvid(raw: str) -> str | None:
    """从任意输入中提取 BV 号（视频页 URL 或裸 BV 号），无则返回 None。"""
    match = re.search(r"BV[0-9A-Za-z]{10}", raw or "")
    return match.group(0) if match else None


class BilibiliClient:
    """PlatformClient 协议的B站实现。全程串行，请求间随机间隔。"""

    def __init__(
        self,
        cookie: str | None = None,
        *,
        http_client: httpx.Client | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], int] | None = None,
    ) -> None:
        self._cookie = cookie or ""
        self._http = http_client or httpx.Client(
            headers={
                "User-Agent": config.USER_AGENT,
                "Referer": config.REFERER,
                "Accept": "application/json",
            },
            timeout=config.REQUEST_TIMEOUT,
        )
        self._rng = rng or random.Random()
        self._sleep = sleep or time.sleep
        self._now = now or (lambda: int(time.time()))
        self._wbi_keys: tuple[str, str] | None = None
        self._view_cache: dict[str, ViewData] = {}

    # ---- 生命周期 ----
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "BilibiliClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- PlatformClient 协议 ----
    def resolve_input(self, raw: str) -> str:
        """URL、BV 号或纯数字 → 集合标识；其余报错。

        识别顺序（技术方案 §6 输入解析规则）：
        1. 纯数字 → 直接作为 season_id；
        2. 链接中含 sid= / season_id= → 提取该值（合集页链接）；
        3. 含 BV 号（视频页 URL 或裸 BV 号）→ 调 view 接口：
           - 属于 UGC 合集 → 返回 season_id；
           - 属于多P视频（videos > 1）→ 返回 bvid 本身，按分P批量提取；
           - 单P且无合集 → 报错；
        4. 其余 → 报错。
        """
        text = (raw or "").strip()
        if text.isdigit():
            return text
        match = re.search(r"[?&](?:sid|season_id)=(\d+)", text)
        if match:
            return match.group(1)
        bvid = extract_bvid(text)
        if bvid:
            return self._resolve_bvid(bvid)
        raise ValueError(
            f"无法从输入中识别合集：{text[:80]!r}。支持合集页链接（含 sid= 或 "
            f"season_id= 参数）、合集内单个视频或多P视频的链接或 BV 号、"
            f"纯数字 season_id。b23.tv 短链暂不支持，请先在浏览器中打开并复制完整链接。"
        )

    def _resolve_bvid(self, bvid: str) -> str:
        view = self._fetch_view(bvid)
        if view.ugc_season and view.ugc_season.id:
            return str(view.ugc_season.id)
        if view.videos > 1:
            return bvid  # 多P视频：以 bvid 作为集合标识，按分P提取
        raise ValueError(
            f"视频 {bvid} 是单P视频且不属于任何合集，没有可批量提取的内容。"
        )

    def _fetch_view(self, bvid: str) -> ViewData:
        """view 接口查询（带缓存，resolve 与 list_episodes 共用）。"""
        if bvid not in self._view_cache:
            try:
                payload = self._api_get(
                    VIEW_URL,
                    params={"bvid": bvid},
                    signed=True,
                    delay_range=config.LIST_DELAY_RANGE,
                )
            except RiskControlError:
                raise
            except BilibiliError as exc:
                raise BilibiliError(f"查询视频 {bvid} 信息失败：{exc}") from exc
            self._view_cache[bvid] = ViewData.model_validate(payload.get("data") or {})
        return self._view_cache[bvid]

    def fetch_season_id_by_bvid(self, bvid: str) -> str:
        """单个视频 → 所属合集 season_id（view 接口的 ugc_season 字段）。

        视频不属于任何合集时抛 ValueError（多P视频除外，见 _resolve_bvid）。
        """
        view = self._fetch_view(bvid)
        season = view.ugc_season
        if season is None or not season.id:
            raise ValueError(
                f"视频 {bvid} 不属于任何合集，无法批量提取。"
                f"请改用合集页链接或 season_id。"
            )
        return str(season.id)

    def list_episodes(self, season_id: str) -> tuple[str, list[Episode]]:
        """取全部"集"，返回（集合名, 分集列表）。

        - season_id（纯数字）：UGC 合集，翻页取全量；
        - bvid（BV 开头）：多P视频，pagelist 的每个分P视作一集（cid 直接可得）。
        """
        if season_id.startswith("BV"):
            return self._list_parts(season_id)
        return self._list_season_episodes(season_id)

    def _list_parts(self, bvid: str) -> tuple[str, list[Episode]]:
        """多P视频：pagelist 全量分P → Episode 列表。"""
        view = self._fetch_view(bvid)
        payload = self._api_get(
            PAGELIST_URL,
            params={"bvid": bvid},
            delay_range=config.LIST_DELAY_RANGE,
        )
        pages = [PageInfo.model_validate(p) for p in (payload.get("data") or [])]
        if not pages:
            raise BilibiliError(f"pagelist 未返回任何分P：{bvid}")
        episodes = [
            Episode(
                bvid=bvid,
                cid=str(p.cid),
                title=p.part or f"P{p.page}",
                index=p.page,
            )
            for p in pages
        ]
        return view.title or bvid, episodes

    def _list_season_episodes(self, season_id: str) -> tuple[str, list[Episode]]:
        """UGC 合集：翻页取全量分集。

        终止条件（任一满足）：达 total；返回空页；整页都与已收集内容重复
        （服务端忽略 page_num 时防死循环；服务端压缩 page_size 时也能翻完）。
        """
        name = ""
        episodes: list[Episode] = []
        seen_bvids: set[str] = set()
        page_num = 1
        total: int | None = None
        while True:
            payload = self._api_get(
                SEASON_ARCHIVES_URL,
                params={"season_id": season_id, "page_num": page_num, "page_size": 100},
                delay_range=config.LIST_DELAY_RANGE,
            )
            page_data = SeasonArchivesPage.model_validate(payload.get("data") or {})
            name = name or page_data.meta.name
            if not page_data.archives:
                break
            if all(item.bvid in seen_bvids for item in page_data.archives):
                break
            for item in page_data.archives:
                if item.bvid in seen_bvids:
                    continue
                seen_bvids.add(item.bvid)
                episodes.append(
                    Episode(
                        bvid=item.bvid,
                        cid=None,
                        title=item.title or item.part or item.bvid,
                        index=len(episodes) + 1,
                    )
                )
            total = int(page_data.page.get("total") or 0) or total
            if total is not None and len(episodes) >= total:
                break
            page_num += 1
        return name, episodes

    def fetch_subtitles(self, episode: Episode) -> SubtitleTrack | None:
        """取该集字幕轨；无任何可用字幕时返回 None。

        选轨优先级（技术方案 §6）：zh-CN（人工CC）> ai-zh > 列表第一个。
        失败（网络/风控/解析）抛 BilibiliError。
        """
        if episode.cid is None:
            episode.cid = self.fetch_cid(episode.bvid)
        payload = self._api_get(
            PLAYER_V2_URL,
            params={"bvid": episode.bvid, "cid": episode.cid},
            signed=True,
            delay_range=config.MEDIA_DELAY_RANGE,
        )
        player = PlayerData.model_validate(payload.get("data") or {})
        subtitles = player.subtitle.subtitles
        if not subtitles:
            return None
        chosen = _choose_track(subtitles)
        if not chosen.subtitle_url:
            raise BilibiliError(f"字幕 {chosen.lan} 缺少 subtitle_url")
        lines = self._download_subtitle_json(chosen.subtitle_url)
        return SubtitleTrack(lan=chosen.lan, lines=lines)

    def fetch_cid(self, bvid: str) -> str:
        """pagelist 取第一分P的 cid。"""
        payload = self._api_get(
            PAGELIST_URL,
            params={"bvid": bvid},
            delay_range=config.LIST_DELAY_RANGE,
        )
        pages = [PageInfo.model_validate(p) for p in (payload.get("data") or [])]
        if not pages:
            raise BilibiliError(f"pagelist 未返回任何分P：{bvid}")
        return str(pages[0].cid)

    # ---- 内部：请求与重试 ----
    def _api_get(
        self,
        url: str,
        *,
        params: dict | None = None,
        signed: bool = False,
        delay_range: tuple[float, float] = config.LIST_DELAY_RANGE,
        ok_codes: tuple[int, ...] = (0,),
        require_code: bool = True,
    ) -> dict:
        """带限速与重试的 API GET，返回完整 JSON payload。

        - 风控信号（HTTP 412/429、业务码 -352/-412）与网络错误：指数退避重试
        - 其余业务错误：直接抛 BilibiliError
        - Cookie 仅随 api.bilibili.com 请求透传（技术方案 §5.3）
        - require_code=False 用于无 code 信封的响应（如字幕 JSON）
        """
        if signed:
            img_key, sub_key = self._get_wbi_keys()
            params = sign(params or {}, img_key, sub_key, self._now())
        # Cookie 只透传到 API 域，不发给字幕 CDN（技术方案 §5.3）
        headers = {"Cookie": self._cookie} if self._cookie and url.startswith(API_BASE) else None
        last_error = "未知错误"
        for attempt in range(config.MAX_RETRIES + 1):
            self._sleep(self._rng.uniform(*delay_range))
            try:
                resp = self._http.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"网络错误（{exc.__class__.__name__}）"
                if attempt < config.MAX_RETRIES:
                    self._sleep(self._backoff(attempt))
                    continue
                raise BilibiliError(f"{last_error}：{url}") from exc
            if resp.status_code in config.RISK_HTTP_STATUS:
                last_error = f"HTTP {resp.status_code}，疑似风控"
                if attempt < config.MAX_RETRIES:
                    self._sleep(self._backoff(attempt))
                    continue
                raise RiskControlError(f"{last_error}（重试 {config.MAX_RETRIES} 次后仍失败）")
            if resp.status_code != 200:
                raise BilibiliError(f"HTTP {resp.status_code}：{url}")
            try:
                payload = resp.json()
            except ValueError:
                last_error = "响应不是合法 JSON"
                if attempt < config.MAX_RETRIES:
                    self._sleep(self._backoff(attempt))
                    continue
                raise BilibiliError(f"{last_error}：{url}") from None
            code = payload.get("code")
            if require_code:
                if isinstance(code, int) and code in config.RISK_BIZ_CODES:
                    last_error = f"业务码 {code}，疑似风控"
                    if attempt < config.MAX_RETRIES:
                        self._sleep(self._backoff(attempt))
                        continue
                    raise RiskControlError(f"{last_error}（重试 {config.MAX_RETRIES} 次后仍失败）")
                if code not in ok_codes:
                    raise BilibiliError(
                        f"业务错误 {code}: {payload.get('message', '')}"
                    )
            return payload
        raise BilibiliError(last_error)

    def _backoff(self, attempt: int) -> float:
        """指数退避 2s/4s/8s + 抖动（技术方案 §5.4）。"""
        return config.RETRY_BACKOFF_BASE * (2**attempt) + self._rng.uniform(
            0, config.RETRY_BACKOFF_BASE
        )

    def _get_wbi_keys(self) -> tuple[str, str]:
        """nav 接口取 wbi 密钥，进程内缓存（技术方案 §5.2）。

        未登录时 nav 返回 -101，但 wbi_img 仍然有效。
        """
        if self._wbi_keys is None:
            payload = self._api_get(
                NAV_URL, ok_codes=(0, -101), delay_range=config.LIST_DELAY_RANGE
            )
            wbi = (payload.get("data") or {}).get("wbi_img") or {}
            img_key = _key_from_url(wbi.get("img_url", ""))
            sub_key = _key_from_url(wbi.get("sub_url", ""))
            if not img_key or not sub_key:
                raise BilibiliError("nav 响应缺少 wbi_img 密钥")
            self._wbi_keys = (img_key, sub_key)
        return self._wbi_keys

    def _download_subtitle_json(self, url: str) -> list[SubtitleLine]:
        """下载字幕 JSON（hdslb.com 域），解析 body 为字幕行。"""
        if url.startswith("//"):
            url = "https:" + url
        payload = self._api_get(
            url, delay_range=config.MEDIA_DELAY_RANGE, require_code=False
        )
        body = payload.get("body")
        if not isinstance(body, list):
            raise BilibiliError(f"字幕 JSON 结构异常（缺少 body 数组）：{url}")
        return [SubtitleLine.model_validate(item) for item in body]


def _choose_track(subtitles):  # noqa: ANN001 - list[SubtitleItem]
    """选轨：zh-CN > ai-zh > 第一个（技术方案 §6 第 4 步）。"""
    for want in ("zh-CN", "ai-zh"):
        for item in subtitles:
            if item.lan == want:
                return item
    return subtitles[0]


def _key_from_url(url: str) -> str:
    """从 wbi 密钥 URL 提取文件名（去扩展名）。"""
    basename = url.rstrip("/").rsplit("/", 1)[-1]
    return basename.rsplit(".", 1)[0] if "." in basename else basename
