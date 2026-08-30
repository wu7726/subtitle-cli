"""RSS 2.0 + iTunes / Podcasting 2.0 命名空间的 feed 解析。

只用标准库 xml.etree（零新增依赖）。只提取本工具需要的字段，
其余一律忽略；解析失败抛 FeedError 由客户端转提示。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pydantic import BaseModel, Field


class FeedError(Exception):
    """feed 结构无效（不是 RSS / 缺 channel）。"""


class TranscriptRef(BaseModel):
    """一条 podcast:transcript 引用。"""

    url: str
    mime: str = ""
    lang: str = ""


class FeedItem(BaseModel):
    """RSS <item>：一集的原始信息。"""

    title: str = ""
    guid: str = ""
    link: str = ""
    pub_date: datetime | None = None
    audio_url: str = ""
    transcripts: list[TranscriptRef] = Field(default_factory=list)


class ParsedFeed(BaseModel):
    """RSS <channel>：节目名、作者与单集列表（feed 原始顺序）。"""

    title: str = ""
    author: str = ""
    items: list[FeedItem] = Field(default_factory=list)


def parse_feed(xml_bytes: bytes) -> ParsedFeed:
    """解析 RSS。title 取 itunes:title > title；author 取 itunes:author > author。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise FeedError(f"RSS 解析失败：{exc}") from exc
    channel = None
    for node in root.iter():
        if _local(node.tag) == "channel":
            channel = node
            break
    if channel is None:
        raise FeedError("不是有效的播客 RSS（未找到 <channel>）")

    feed = ParsedFeed()
    for child in channel:
        tag = _local(child.tag)
        text = (child.text or "").strip()
        if tag == "title" and not feed.title:
            feed.title = text
        elif tag == "author" and not feed.author:
            feed.author = text
        elif tag == "item":
            feed.items.append(_parse_item(child))
    return feed


def _parse_item(item: ET.Element) -> FeedItem:
    entry = FeedItem()
    for child in item:
        tag = _local(child.tag)
        text = (child.text or "").strip()
        if tag == "title" and not entry.title:
            entry.title = text
        elif tag == "guid" and not entry.guid:
            entry.guid = text
        elif tag == "link" and not entry.link:
            entry.link = text
        elif tag == "pubdate" and entry.pub_date is None:
            entry.pub_date = _parse_date(text)
        elif tag == "enclosure" and not entry.audio_url:
            entry.audio_url = child.get("url") or ""
        elif tag == "transcript":
            url = child.get("url") or ""
            if url:
                entry.transcripts.append(
                    TranscriptRef(
                        url=url,
                        mime=(child.get("type") or "").strip(),
                        lang=(child.get("lang") or "").strip(),
                    )
                )
    return entry


def sort_items_chronological(items: list[FeedItem]) -> list[FeedItem]:
    """按发布时间升序（最早的为第 1 集）；无日期的排在有日期之后，保持稳定。"""
    return sorted(items, key=lambda item: (item.pub_date is None, item.pub_date or datetime.min))


def _parse_date(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None


def _local(tag: str) -> str:
    """剥掉命名空间：{https://...}transcript → transcript。"""
    return tag.rpartition("}")[2].lower()
