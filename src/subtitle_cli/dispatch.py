"""平台分派：识别输入属于哪个平台并创建对应客户端。

判定顺序很关键：先匹配B站特征（bilibili.com / b23.tv / BV 号 / 纯数字），
再匹配播客特征（小宇宙 / Apple / 其余 http(s) 链接按 RSS 尝试），
都不匹配时回落B站（由其 resolve_input 给出与旧版一致的报错）。
"""

from __future__ import annotations

import re

from .bilibili.client import BilibiliClient
from .bilibili.client import extract_bvid
from .douyin.client import DouyinClient
from .pipeline import PlatformClient
from .podcast.client import PodcastClient
from .vault import VaultConfig

BILIBILI = "bilibili"
PODCAST = "podcast"
DOUYIN = "douyin"


def detect_platform(raw: str) -> str:
    """输入 → 平台标识（bilibili / podcast / douyin）。永远有返回值，不抛。"""
    text = (raw or "").strip()
    if "bilibili.com" in text or "b23.tv" in text:
        return BILIBILI
    if "douyin.com" in text or "iesdouyin.com" in text:
        return DOUYIN
    if re.fullmatch(r"\d{15,20}", text or ""):
        return DOUYIN  # 抖音视频 ID（15-20 位）；B站 season_id 更短
    if extract_bvid(text) or text.isdigit():
        return BILIBILI
    if "xiaoyuzhoufm.com" in text or "podcasts.apple.com" in text:
        return PODCAST
    if re.match(r"^https?://", text):
        return PODCAST
    return BILIBILI


def create_client(raw: str, cookie: str | None = None) -> PlatformClient:
    """按输入平台创建客户端；Cookie 只对B站有意义，其他平台直接忽略。"""
    platform = detect_platform(raw)
    if platform == PODCAST:
        return PodcastClient()
    if platform == DOUYIN:
        return DouyinClient()
    return BilibiliClient(cookie=cookie)


def platform_subdir(cfg: VaultConfig, platform: str) -> str:
    """该平台在 vault 内的子目录（播客/抖音各有专属默认，B站用 subdir）。"""
    if platform == PODCAST:
        return cfg.podcast_subdir
    if platform == DOUYIN:
        return cfg.douyin_subdir
    return cfg.subdir


def set_platform_subdir(cfg: VaultConfig, platform: str, value: str) -> None:
    """把用户显式指定的子目录记到对应平台字段上（CLI 与网页共用）。"""
    if platform == PODCAST:
        cfg.podcast_subdir = value
    elif platform == DOUYIN:
        cfg.douyin_subdir = value
    else:
        cfg.subdir = value
