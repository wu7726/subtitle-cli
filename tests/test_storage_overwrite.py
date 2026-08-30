"""storage 增量单测（M7）：覆盖写语义与 Obsidian 双链保留字符清洗。"""

from __future__ import annotations

from pathlib import Path

import pytest

from subtitle_cli import storage


def test_write_markdown_overwrite_semantics(tmp_path: Path):
    p = tmp_path / "t.md"
    storage.write_markdown(p, "第一版\n")
    with pytest.raises(FileExistsError):
        storage.write_markdown(p, "第二版\n")
    storage.write_markdown(p, "第二版\n", overwrite=True)  # 仅索引页等自有产物使用
    assert p.read_text(encoding="utf-8") == "第二版\n"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a#b[c]d^e", "a_b_c_d_e"),  # Obsidian 双链保留字符 → 下划线
        ("EP01 [专题] 名", "EP01 _专题_ 名"),
    ],
)
def test_sanitize_obsidian_link_chars(raw: str, expected: str):
    assert storage.sanitize_filename(raw) == expected
