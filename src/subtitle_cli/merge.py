"""合集全文合并导出：把分集笔记拼成一个长文档，整卷喂给 AI 总结。

只读产物、剥离属性头、按 EP 序号（数值）排序；输出落在合集目录内
`<合集名>-全文.md`。合并产物不参与双链索引与增量跳过（storage/本模块
对 `-全文` 后缀双侧豁免），重复合并结果幂等。
"""

from __future__ import annotations

import re
from pathlib import Path

from subtitle_cli import storage

_EP_NUM = re.compile(r"EP(\d+)")


def _ep_sort_key(path: Path) -> tuple[int, str]:
    """EP 序号数值排序——EP100 必须排在 EP11 之后（字典序会排反）。"""
    match = _EP_NUM.search(path.stem)
    return (int(match.group(1)) if match else 10**9, path.stem)


def _strip_frontmatter(text: str) -> str:
    """去掉笔记头部的 YAML 属性头（合并文档不需要逐集属性）。"""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end < 0:
        return text
    return text[end + 4:].lstrip("\n")


def merge_collection(collection_dir: Path, collection_name: str) -> Path:
    """合并合集目录下的分集笔记，返回输出路径；没有可合并分集时抛 ValueError。"""
    index_stem = storage.collection_dirname(collection_name)
    target = collection_dir / f"{index_stem}-全文.md"
    sections: list[str] = []
    for md in sorted(collection_dir.glob("EP*.md"), key=_ep_sort_key):
        if md.stem == index_stem or md.stem.endswith("-全文"):
            continue  # 索引页与本产物自身不参与
        body = _strip_frontmatter(md.read_text(encoding="utf-8")).strip()
        if not body:
            continue
        sections.append(f"## {md.stem}\n\n{body}\n")
    if not sections:
        raise ValueError(f"合集目录里没有可合并的分集笔记：{collection_dir}")
    header = (
        f"# {collection_name}（全文合并）\n\n"
        f"> 由 subtitle-cli 合并生成：共 {len(sections)} 集，正文一字未改，仅按分集分节。\n"
    )
    target.write_text(header + "\n" + "\n".join(sections), encoding="utf-8")
    return target


def merge_outcome(outcome, output_dir: Path) -> Path:
    """按运行结果定位合集目录并合并（CLI --merge 与网页 /api/merge 共用）。"""
    return merge_collection(
        Path(output_dir) / storage.collection_dirname(outcome.collection_name),
        outcome.collection_name,
    )
