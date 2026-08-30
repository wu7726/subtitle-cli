"""平台分派：识别输入属于哪个平台并创建对应客户端。

判定顺序很关键：先匹配B站特征（bilibili.com / b23.tv / BV 号 / 纯数字），
再匹配播客特征（小宇宙 / Apple / 其余 http(s) 链接按 RSS 尝试），
都不匹配时回落B站（由其 resolve_input 给出与旧版一致的报错）。
"""

from __future__ import annotations

import re

from .bilibili.client import BilibiliClient
from .bilibili.client import extract_bvid
from .pipeline import PlatformClient
from .podcast.client import PodcastClient

BILIBILI = "bilibili"
PODCAST = "podcast"


def detect_platform(raw: str) -> str:
    """输入 → 平台标识（bilibili / podcast）。永远有返回值，不抛。"""
    text = (raw or "").strip()
    if "bilibili.com" in text or "b23.tv" in text:
        return BILIBILI
    if extract_bvid(text) or text.isdigit():
        return BILIBILI
    if "xiaoyuzhoufm.com" in text or "podcasts.apple.com" in text:
        return PODCAST
    if re.match(r"^https?://", text):
        return PODCAST
    return BILIBILI


def create_client(raw: str, cookie: str | None = None) -> PlatformClient:
    """按输入平台创建客户端；Cookie 只对B站有意义，播客侧直接忽略。"""
    if detect_platform(raw) == PODCAST:
        return PodcastClient()
    return BilibiliClient(cookie=cookie)
