"""文件名清洗、落盘与增量判断（技术方案 §6 文件名规则）。"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable

from . import config, notes

# Windows 文件名非法字符 + Obsidian 双链保留字符（# 锚点、[] 链接语法、
# ^ 块引用——出现在文件名里会让索引页双链永远失配，开发计划 §2.3）
_ILLEGAL_CHARS = '<>:"/\\|?*#[]^'
# Windows 保留名（不区分大小写，含带扩展名形式如 CON.txt）
_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def sanitize_filename(name: str) -> str:
    """清洗单个文件名/目录名片段，保证 Windows 合法。"""
    # 去掉控制字符
    name = "".join(ch for ch in name if ord(ch) >= 32)
    # 非法字符替换为 _
    for ch in _ILLEGAL_CHARS:
        name = name.replace(ch, "_")
    # 去除首尾空白与尾部的空格、点
    name = name.strip().rstrip(" .")
    if not name:
        return "_"
    # Windows 保留名改写：前缀 _（覆盖 CON、con.txt 两种形态）
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name


def truncate_title(title: str) -> str:
    """标题截断至固定长度，保证整路径远低于 260（技术方案 §6）。"""
    return title[: config.TITLE_MAX_CHARS]


def episode_filename(index: int, title: str) -> str:
    """分集文件名：EP{index:02d} {title}.md。"""
    safe = sanitize_filename(truncate_title(title.strip()))
    return f"EP{index:02d} {safe}.md"


def collection_dirname(name: str) -> str:
    """合集名作为子目录，同样需要清洗。"""
    return sanitize_filename(name)


def output_path(output_root: Path, collection_name: str, index: int, title: str) -> Path:
    """某集的目标落盘路径：<output>/<合集名>/EP{index:02d} {title}.md。"""
    return (
        Path(output_root)
        / collection_dirname(collection_name)
        / episode_filename(index, title)
    )


def is_downloaded(path: Path) -> bool:
    """增量判断：文件存在且非空（技术方案 §6 第 1 步）。"""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def write_markdown(path: Path, content: str, *, overwrite: bool = False) -> None:
    """以 UTF-8 + LF 落盘；目标已存在时抛 FileExistsError（增量保护）。

    overwrite=True 仅用于索引页重生成等工具自有产物（PRD §7 写入纪律）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    with open(path, mode, encoding="utf-8", newline="\n") as f:
        f.write(content)


_EP_STEM = re.compile(r"EP(\d+)\s+(.+)")


def write_collection_index(
    collection_dir: Path,
    collection_name: str,
    season_id: str | None,
    fetched_at: date,
    log: Callable[[str], None] = print,
) -> Path | None:
    """重生成合集索引页：条目从磁盘实况收集，保证双链与文件名永远一致
    （开发计划 §2.3）。合集目录尚不存在时跳过；每次整页覆盖重写。

    pipeline（obsidian 提取）与 migration（离线迁移）共用本函数。
    """
    if not collection_dir.is_dir():
        return None
    index_stem = collection_dirname(collection_name)
    found: list[tuple[int, str, str]] = []
    for md in collection_dir.glob("EP*.md"):
        if md.stem == index_stem:  # 合集名以 EP 开头时避免把索引自收录
            continue
        match = _EP_STEM.fullmatch(md.stem)
        if match:
            found.append((int(match.group(1)), md.stem, match.group(2)))
    found.sort(key=lambda item: item[0])
    entries = [
        notes.IndexEntry(stem=stem, alias=f"第{idx}集 {title}")
        for idx, stem, title in found
    ]
    index_name = f"{index_stem}.md"
    path = collection_dir / index_name
    content = notes.build_index_note(collection_name, season_id, entries, fetched_at)
    write_markdown(path, content, overwrite=True)
    log(f"索引页已更新：{index_name}（{len(entries)} 集）")
    return path
