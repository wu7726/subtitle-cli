"""storage 单测：文件名清洗/保留名/截断/增量判断/落盘（技术方案 §6 文件名规则）。"""

import pytest

from subtitle_cli import storage
from subtitle_cli.bilibili.models import Episode
from subtitle_cli.pipeline import episode_heading
from pathlib import Path


# ---- 文件名清洗 ----
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
        ("尾随空格   。. .", "尾随空格   。"),  # 只去尾部空格与点，正文不受影响
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("LPT1", "_LPT1"),
        ("正常的标题.mp4", "正常的标题.mp4"),
        ("a\x01b\x1fc", "abc"),  # 控制字符剔除
        ("<<<", "___"),  # 全非法字符 → 全下划线
    ],
)
def test_sanitize_filename(raw: str, expected: str):
    assert storage.sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", " . ", "\x01\x02\x03"])
def test_sanitize_empty_becomes_underscore(raw: str):
    assert storage.sanitize_filename(raw) == "_"


def test_truncate_title():
    long_title = "长" * 100
    assert len(storage.truncate_title(long_title)) == storage.config.TITLE_MAX_CHARS


def test_episode_filename_format():
    assert storage.episode_filename(1, "第一集 标题") == "EP01 第一集 标题.md"
    assert storage.episode_filename(58, "标题") == "EP58 标题.md"
    assert storage.episode_filename(100, "标题") == "EP100 标题.md"


def test_episode_filename_sanitizes_and_truncates():
    name = storage.episode_filename(3, "a/b\\c" + "长" * 100)
    assert "/" not in name and "\\" not in name
    assert name.startswith("EP03 a_b_c") and name.endswith(".md")


def test_output_path_joins_collection_dir(tmp_path: Path):
    path = storage.output_path(tmp_path, '合集:名字', 2, "标题")
    assert path == tmp_path / "合集_名字" / "EP02 标题.md"


# ---- 增量判断 ----
def test_is_downloaded(tmp_path: Path):
    missing = tmp_path / "a.md"
    assert storage.is_downloaded(missing) is False

    empty = tmp_path / "b.md"
    empty.write_text("", encoding="utf-8")
    assert storage.is_downloaded(empty) is False

    full = tmp_path / "c.md"
    full.write_text("内容", encoding="utf-8")
    assert storage.is_downloaded(full) is True


# ---- 落盘 ----
def test_write_markdown_utf8_lf(tmp_path: Path):
    path = tmp_path / "sub" / "EP01 测试.md"
    storage.write_markdown(path, "# 标题\n\n第一段\n")
    raw = path.read_bytes()
    assert b"\r" not in raw  # newline="\n"：不得出现 CRLF
    assert path.read_text(encoding="utf-8") == "# 标题\n\n第一段\n"


def test_write_markdown_refuses_overwrite(tmp_path: Path):
    path = tmp_path / "EP01 测试.md"
    storage.write_markdown(path, "第一版")
    with pytest.raises(FileExistsError):
        storage.write_markdown(path, "第二版")
    assert path.read_text(encoding="utf-8") == "第一版"


# ---- Markdown 一级标题规则（pipeline.episode_heading）----
def test_episode_heading_adds_prefix():
    ep = Episode(bvid="BV1", index=1, title="视频标题")
    assert episode_heading(ep) == "第1集 视频标题"


def test_episode_heading_avoids_duplicate_prefix():
    ep = Episode(bvid="BV1", index=1, title="第1集 视频标题")
    assert episode_heading(ep) == "第1集 视频标题"
