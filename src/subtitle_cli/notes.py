"""Obsidian 笔记层：YAML 属性头、合集索引页、wikilink（PRD F2/F3）。

纯函数、确定性、无 I/O、无网络（开发计划 §0）。converter 产出的正文在
本层原样外层包装，一字不动。YAML 为手写序列化：特殊值双引号包裹
（借 json.dumps 转义，YAML 双引号标量兼容 JSON 转义），宁过度引号不欠。
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import NamedTuple

from pydantic import BaseModel

FETCHED_BY = "subtitle-cli"  # 溯源字段，同时是迁移「已处理」判定标记
BASE_TAG = "B站字幕"
INDEX_TAG = "索引"


class EpisodeMeta(BaseModel):
    """一集笔记的属性头来源。多P 视频 season_id 为空且省略该键。"""

    title: str  # 一级标题文本，如「第1集 视频标题」
    collection: str
    season_id: str | None = None
    bvid: str
    episode_index: int
    is_multi_p: bool = False
    fetched_at: date


class IndexEntry(NamedTuple):
    """索引页里的一个双链条目：stem 为文件名去扩展名（双链目标）。"""

    stem: str
    alias: str


def episode_url(bvid: str, index: int, *, is_multi_p: bool) -> str:
    """视频链接；多P 追加 ?p=序号（开发计划 §2.1）。"""
    url = f"https://www.bilibili.com/video/{bvid}"
    return f"{url}?p={index}" if is_multi_p else url


# 无需引号即可安全内联的标量：字母/数字/下划线/CJK（含中文标点全角区）开头，
# 其后允许空格与少量安全符号。半角冒号、引号、#、花括号等一律引号包裹。
_PLAIN_SAFE = re.compile(
    r"^[\w\u3000-\u9fff\uff00-\uffef][\w\u3000-\u9fff\uff00-\uffef\s./+()（）·、，。！？—…-]*$"
)
_NUMERIC = re.compile(r"^[+-]?\d+(\.\d+)?$")
_BOOL_NULL = {"true", "false", "null", "yes", "no", "on", "off", "~"}


def _yaml_scalar(value: str) -> str:
    """标量 → YAML 内联文本。数字形/布尔形/含特殊字符的值加双引号。"""
    if value == "":
        return '""'
    safe = (
        _PLAIN_SAFE.fullmatch(value) is not None
        and value == value.strip()
        and not value.endswith(":")
        and _NUMERIC.fullmatch(value) is None
        and value.lower() not in _BOOL_NULL
    )
    if safe:
        return value
    return json.dumps(value, ensure_ascii=False)


def _tag_lines(tags: list[str]) -> list[str]:
    lines = ["tags:"]
    lines.extend(f"  - {_yaml_scalar(tag)}" for tag in tags)
    return lines


def _frontmatter(meta: EpisodeMeta) -> list[str]:
    """属性头键序固定（开发计划 M5）：缺字段的行为由 validate_meta 另行报告。"""
    lines = [
        f"title: {_yaml_scalar(meta.title)}",
        f"collection: {_yaml_scalar(meta.collection)}",
    ]
    if not meta.is_multi_p and meta.season_id:
        lines.append(f"season_id: {_yaml_scalar(meta.season_id)}")
    lines.extend(
        [
            f"bvid: {_yaml_scalar(meta.bvid)}",
            f"episode: {meta.episode_index}",
            f"url: {_yaml_scalar(episode_url(meta.bvid, meta.episode_index, is_multi_p=meta.is_multi_p))}",
            f"fetched_at: {meta.fetched_at.isoformat()}",
            f"fetched_by: {FETCHED_BY}",
        ]
    )
    lines.extend(_tag_lines([BASE_TAG, meta.collection]))
    lines.append("---")  # 收栏
    return lines


def build_episode_note(meta: EpisodeMeta, body_md: str) -> str:
    """属性头 + 正文。body_md 原样拼接（converter 契约：结尾已带一个换行）。"""
    return "---\n" + "\n".join(_frontmatter(meta)) + "\n\n" + body_md


def build_index_note(
    collection: str,
    season_id: str | None,
    entries: list[IndexEntry],
    fetched_at: date,
) -> str:
    """合集索引页：type: index + 全部分集双链。条目由调用方从磁盘实况生成。"""
    lines = [
        "type: index",
        f"collection: {_yaml_scalar(collection)}",
    ]
    if season_id:
        lines.append(f"season_id: {_yaml_scalar(season_id)}")
    lines.extend(
        [
            f"episodes: {len(entries)}",
            f"updated: {fetched_at.isoformat()}",
        ]
    )
    lines.extend(_tag_lines([BASE_TAG, INDEX_TAG]))
    lines.append("---")  # 收栏
    body = [f"# {collection}", ""]
    body.extend(f"- {wikilink(stem, alias)}" for stem, alias in entries)
    return "---\n" + "\n".join(lines) + "\n\n" + "\n".join(body).rstrip("\n") + "\n"


# Obsidian 双链保留字符：| 别名分隔、# 锚点、[] 链接语法、^ 块引用。
# 文件名层（storage）保证不出现这些字符，这里仅作防御性兜底。
_WIKILINK_UNSAFE = re.compile(r"[|#^\[\]]")


def wikilink(stem: str, alias: str) -> str:
    """[[目标|别名]] 双链。目标与别名中的保留字符替换为空格。"""
    return f"[[{_WIKILINK_UNSAFE.sub(' ', stem)}|{_WIKILINK_UNSAFE.sub(' ', alias)}]]"


_LIST_ITEM = re.compile(r"^\s+-\s*(.+)$")
_KEY_VALUE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")


def _unyaml(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def parse_frontmatter(text: str) -> dict[str, object] | None:
    """解析本工具产出的简单 YAML（标量 + 单层列表）。

    无属性头或未闭合返回 None；遇到不认识的形态返回 None（调用方按
    「非本工具笔记」处理，如迁移时视为旧格式）。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None

    result: dict[str, object] = {}
    pending_list_key: str | None = None
    for raw in lines[1:end]:
        if not raw.strip():
            continue
        item = _LIST_ITEM.match(raw)
        if item:
            if pending_list_key is None:
                return None
            result[pending_list_key].append(_unyaml(item.group(1)))  # type: ignore[union-attr]
            continue
        kv = _KEY_VALUE.match(raw)
        if not kv:
            return None
        key, value = kv.group(1), kv.group(2)
        if value == "":
            result[key] = []
            pending_list_key = key
        else:
            result[key] = _unyaml(value)
            pending_list_key = None
    return result


def has_migration_marker(text: str) -> bool:
    """笔记是否已由本工具写入（frontmatter 含 fetched_by 标记）。"""
    fm = parse_frontmatter(text)
    return isinstance(fm, dict) and fm.get("fetched_by") == FETCHED_BY


def validate_meta(meta: EpisodeMeta) -> list[str]:
    """缺失字段清单（键名）；url 由 bvid 派生，不单独检查。"""
    missing: list[str] = []
    if not meta.title.strip():
        missing.append("title")
    if not meta.collection.strip():
        missing.append("collection")
    if not meta.bvid.strip():
        missing.append("bvid")
    if meta.episode_index < 1:
        missing.append("episode")
    if not meta.is_multi_p and not (meta.season_id or "").strip():
        missing.append("season_id")
    return missing
