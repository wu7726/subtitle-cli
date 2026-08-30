"""notes 单测：属性头序列化/索引页/wikilink/迁移标记（开发计划 M5 + v1.2 属性模板）。

锁定三条硬约束：
1. 属性键对齐 Obsidian Web Clipper 模板（author/created/description/published/
   source/tags/title，fetched_by 内部标记殿后）；
2. 正文一字不动——build_episode_note 只在成品外层包装；
3. parse_frontmatter 与手写序列化互为 roundtrip。
"""

import json
from datetime import date

from subtitle_cli.notes import (
    FETCHED_BY,
    EpisodeMeta,
    build_episode_note,
    build_index_note,
    episode_tags,
    episode_url,
    has_migration_marker,
    parse_frontmatter,
    validate_meta,
    wikilink,
)


def meta(**kw) -> EpisodeMeta:
    base = dict(
        title="第1集 视频标题",
        source="https://www.bilibili.com/video/BV1abc",
        author="演示UP主",
        created=date(2026, 8, 30),
        tags=["B站字幕", "美食漫谈"],
        collection="美食漫谈",
    )
    base.update(kw)
    return EpisodeMeta(**base)


def test_episode_note_snapshot():
    out = build_episode_note(meta(), "# 第1集 视频标题\n\n大家好。\n")
    assert out == (
        "---\n"
        "author: 演示UP主\n"
        "created: 2026-08-30\n"
        'description: ""\n'
        'published: ""\n'
        'source: "https://www.bilibili.com/video/BV1abc"\n'
        "tags:\n"
        "  - B站字幕\n"
        "  - 美食漫谈\n"
        "title: 第1集 视频标题\n"
        f"fetched_by: {FETCHED_BY}\n"
        "---\n"
        "\n"
        "# 第1集 视频标题\n"
        "\n"
        "大家好。\n"
    )


def test_multi_p_source_carries_page_param():
    out = build_episode_note(
        meta(source=episode_url("BV1abc", 2, is_multi_p=True)), "# 第2P 标题\n"
    )
    assert 'source: "https://www.bilibili.com/video/BV1abc?p=2"' in out


def test_episode_url_branches():
    assert episode_url("BV1x", 1, is_multi_p=False) == "https://www.bilibili.com/video/BV1x"
    assert episode_url("BV1x", 3, is_multi_p=True) == "https://www.bilibili.com/video/BV1x?p=3"


def test_episode_tags():
    assert episode_tags("美食漫谈") == ["B站字幕", "美食漫谈"]
    assert episode_tags("  ") == ["B站字幕"]


def test_body_passed_through_unchanged():
    body = "# 标题\n\n第一段：带冒号\"引号\"与 # 井号。\n\n第二段。\n"
    out = build_episode_note(meta(), body)
    assert out.endswith(body)


def test_title_with_special_chars_roundtrip():
    tricky = '第1集: 话题"两难" #2'
    out = build_episode_note(meta(title=tricky), "# t\n")
    fm = parse_frontmatter(out)
    assert fm is not None and fm["title"] == tricky


def test_numeric_looking_strings_stay_strings():
    out = build_episode_note(meta(title="2024", author="2048"), "# t\n")
    fm = parse_frontmatter(out)
    assert fm is not None
    assert fm["title"] == "2024"
    assert fm["author"] == "2048"


def test_yaml_scalar_uses_json_escaping():
    # 引号路径输出必须是合法 JSON 字符串，才可被 _unyaml 还原
    from subtitle_cli.notes import _yaml_scalar

    raw = 'a"b\\c\n中文'
    assert _yaml_scalar(raw) == json.dumps(raw, ensure_ascii=False)


def test_index_note_snapshot():
    out = build_index_note(
        "美食漫谈",
        "12345",
        [
            ("EP01 第一集标题", "第1集 第一集标题"),
            ("EP02 第二集标题", "第2集 第二集标题"),
        ],
        date(2026, 8, 30),
    )
    assert out == (
        "---\n"
        "type: index\n"
        "collection: 美食漫谈\n"
        'season_id: "12345"\n'
        "episodes: 2\n"
        "updated: 2026-08-30\n"
        "tags:\n"
        "  - B站字幕\n"
        "  - 索引\n"
        "---\n"
        "\n"
        "# 美食漫谈\n"
        "\n"
        "- [[EP01 第一集标题|第1集 第一集标题]]\n"
        "- [[EP02 第二集标题|第2集 第二集标题]]\n"
    )


def test_index_note_multi_p_omits_season_id_and_empty_entries():
    out = build_index_note("单视频多P", None, [], date(2026, 8, 30))
    assert "season_id" not in out
    assert "episodes: 0" in out
    assert out.endswith("# 单视频多P\n")


def test_index_note_roundtrip():
    out = build_index_note(
        "合集:带引号", None, [("EP01 t", "第1集 t")], date(2026, 8, 30)
    )
    fm = parse_frontmatter(out)
    assert fm is not None
    assert fm["type"] == "index"
    assert fm["collection"] == "合集:带引号"
    assert fm["tags"] == ["B站字幕", "索引"]


def test_wikilink_sanitizes_reserved_chars():
    assert wikilink("EP01 [特] #题|名", "第1集 别|名") == "[[EP01  特   题 名|第1集 别 名]]"


def test_parse_frontmatter_variants():
    assert parse_frontmatter("没有属性头") is None
    assert parse_frontmatter("---\ntitle: 未闭合\n") is None
    assert parse_frontmatter("---\n未知形态: [a, b]\n---\n") is None  # 内联列表不支持
    assert parse_frontmatter("") is None
    fm = parse_frontmatter("---\ntitle: 普通\ncount: 3\n---\n\n# t\n")
    assert fm == {"title": "普通", "count": "3"}


def test_migration_marker_positive_and_negative():
    note = build_episode_note(meta(), "# t\n")
    assert has_migration_marker(note)
    assert not has_migration_marker("# 普通旧格式笔记\n\n正文\n")
    assert not has_migration_marker("---\nfetched_by: other-tool\n---\n\n# t\n")


def test_validate_meta_missing_fields():
    assert validate_meta(meta()) == []
    assert validate_meta(meta(source="")) == ["source"]
    assert validate_meta(meta(title="")) == ["title"]
    assert validate_meta(meta(tags=[])) == ["tags"]
