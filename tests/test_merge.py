"""合集全文合并导出单测：排序、属性头剥离、自排除、幂等。全程离线。"""

from pathlib import Path

import pytest

from subtitle_cli.merge import merge_collection


def _write_note(directory: Path, name: str, body: str, frontmatter: bool = False) -> None:
    text = (f"---\ntitle: {name}\ntags:\n  - B站字幕\n---\n\n{body}" if frontmatter else body)
    (directory / name).write_text(text, encoding="utf-8")


def test_merge_orders_numerically_and_strips_frontmatter(tmp_path: Path):
    """EP100 必须排在 EP11 之后（数值序）；属性头剥离；索引自排除。"""
    _write_note(tmp_path, "EP02 二.md", "第二集正文。")
    _write_note(tmp_path, "EP11 十一.md", "第十一集正文。", frontmatter=True)
    _write_note(tmp_path, "EP100 一百.md", "第一百集正文。")
    _write_note(tmp_path, "测试合集.md", "- [[EP02 二|第2集 二]]")  # 索引页

    target = merge_collection(tmp_path, "测试合集")
    assert target.name == "测试合集-全文.md"
    content = target.read_text(encoding="utf-8")
    assert "# 测试合集（全文合并）" in content
    assert content.index("## EP02 二") < content.index("## EP11 十一") < content.index("## EP100 一百")
    assert "第二集正文。" in content
    assert "title:" not in content  # 属性头已剥离
    assert "[[EP02 二" not in content  # 索引页不参与


def test_merge_skips_previous_output_idempotent(tmp_path: Path):
    """重复合并幂等：上一次的 -全文 产物不作为输入，两次内容一致。"""
    _write_note(tmp_path, "EP01 一.md", "第一集。")
    first = merge_collection(tmp_path, "测试合集")
    first_bytes = first.read_bytes()
    second = merge_collection(tmp_path, "测试合集")
    assert second == first
    assert second.read_bytes() == first_bytes


def test_merge_empty_collection_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="没有可合并的分集笔记"):
        merge_collection(tmp_path, "测试合集")
