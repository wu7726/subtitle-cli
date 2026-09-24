"""state 单测：单集稳定键与按合集状态文件（优化方案 刀1）。

所有落盘函数通过显式 root 参数注入 tmp 目录，绝不触碰真实 HOME。
"""

import json
from pathlib import Path

from subtitle_cli.bilibili.models import Episode, EpisodeStatus
from subtitle_cli.state import (
    STATE_VERSION,
    CollectionState,
    collection_state_path,
    episode_key,
    key_is_positional,
    load_collection,
    record_episode,
    save_collection,
    status_of,
)


def _episode(**overrides) -> Episode:
    fields = dict(bvid="BV1abc", cid=None, title="第一集 绪论", index=1)
    fields.update(overrides)
    return Episode(**fields)


# ---- 单集稳定键 ----


def test_episode_key_prefers_cid():
    # B站多P：所有分P 共用 bvid，只有 cid 能区分
    p1 = _episode(bvid="BV1multi", cid="101", index=1)
    p2 = _episode(bvid="BV1multi", cid="102", index=2)
    assert episode_key(p1) == "101"
    assert episode_key(p2) == "102"
    assert episode_key(p1) != episode_key(p2)


def test_episode_key_falls_back_to_bvid():
    # 合集/抖音：cid 在列表阶段为 None，bvid 本来就唯一
    assert episode_key(_episode(bvid="BV1abc", cid=None)) == "BV1abc"
    assert episode_key(_episode(bvid="aweme_7321", cid=None)) == "aweme_7321"


def test_key_is_positional_only_for_podcast_fallback():
    assert key_is_positional(_episode(bvid="podcast-3", cid=None))
    assert not key_is_positional(_episode(bvid="podcast-3", cid="5"))  # 有 cid 就可靠
    assert not key_is_positional(_episode(bvid="BV1abc", cid=None))
    assert not key_is_positional(_episode(bvid="guid-xyz", cid=None))


# ---- 状态文件路径 ----


def test_state_path_sanitizes_feed_url(tmp_path):
    path = collection_state_path("https://example.com/feed.xml?a=1&b=2", root=tmp_path)
    assert path.parent == tmp_path
    assert path.suffix == ".json"
    for illegal in '<>:"/\\|?*#[]^':
        assert illegal not in path.name


def test_state_path_empty_season_id(tmp_path):
    path = collection_state_path("   ", root=tmp_path)
    assert path.name == "_.json"


# ---- 落点指纹：同一合集在不同输出目录各自独立 ----


def test_state_path_differs_by_output_dir(tmp_path):
    a = collection_state_path("123", tmp_path / "A", tmp_path)
    b = collection_state_path("123", tmp_path / "B", tmp_path)
    assert a != b
    assert a.name.startswith("123-") and b.name.startswith("123-")


def test_state_path_same_dir_same_fingerprint(tmp_path):
    """同一落点的不同写法（大小写、多余分隔符）必须指向同一个状态文件。"""
    target = tmp_path / "Vault"
    target.mkdir()
    a = collection_state_path("123", target, tmp_path)
    b = collection_state_path("123", str(target).upper(), tmp_path)
    c = collection_state_path("123", Path(str(target) + "/"), tmp_path)
    assert a == b == c


def test_load_ignores_other_target_state(tmp_path):
    """换个落点再跑：读不到旧落点的记录（否则新目录会永远是空的）。"""
    state = CollectionState(season_id="123", output_dir=str(tmp_path / "A"))
    record_episode(state, _episode(bvid="BV1", index=1), EpisodeStatus.SUCCESS)
    save_collection(state, tmp_path)

    assert load_collection("123", tmp_path / "A", tmp_path).episodes  # 同落点读得到
    assert load_collection("123", tmp_path / "B", tmp_path).episodes == {}


def test_load_missing_returns_empty_state(tmp_path):
    state = load_collection("123", tmp_path, tmp_path)
    assert state.season_id == "123"
    assert state.episodes == {}
    assert state.version == STATE_VERSION
    assert state.output_dir  # 记下落点，便于 status 展示


def test_record_and_roundtrip(tmp_path):
    state = CollectionState(
        season_id="123", output_dir=str(tmp_path), collection_name="数字信号处理"
    )
    record_episode(state, _episode(bvid="BV1", index=1), EpisodeStatus.SUCCESS)
    record_episode(
        state, _episode(bvid="BV2", index=2), EpisodeStatus.FAILED, reason="风控"
    )
    save_collection(state, tmp_path)

    loaded = load_collection("123", tmp_path, tmp_path)
    assert loaded.collection_name == "数字信号处理"
    assert loaded.output_dir == str(tmp_path)
    assert loaded.episodes["BV1"].status == EpisodeStatus.SUCCESS
    assert loaded.episodes["BV2"].status == EpisodeStatus.FAILED
    assert loaded.episodes["BV2"].reason == "风控"
    assert loaded.updated  # save 时刷新


def test_record_preserves_other_episodes(tmp_path):
    state = CollectionState(season_id="123")
    record_episode(state, _episode(bvid="BV1", index=1), EpisodeStatus.SUCCESS)
    record_episode(state, _episode(bvid="BV2", index=2), EpisodeStatus.FAILED)
    # EP2 失败后重跑成功：只覆盖 EP2，EP1 的记录原样保留
    record_episode(state, _episode(bvid="BV2", index=2), EpisodeStatus.SUCCESS)
    assert state.episodes["BV1"].status == EpisodeStatus.SUCCESS
    assert state.episodes["BV2"].status == EpisodeStatus.SUCCESS


def test_save_leaves_no_tmp_behind(tmp_path):
    state = CollectionState(season_id="123")
    record_episode(state, _episode(), EpisodeStatus.SUCCESS)
    save_collection(state, tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob("*.json"))


def test_load_corrupt_json_returns_empty(tmp_path):
    path = collection_state_path("123", tmp_path, tmp_path)
    path.write_text("{not json", encoding="utf-8")
    state = load_collection("123", tmp_path, tmp_path)
    assert state.episodes == {}


def test_load_season_id_mismatch_returns_empty(tmp_path):
    # 截断碰撞防御：文件里的 season_id 对不上就当没有记录
    path = collection_state_path("123", tmp_path, tmp_path)
    path.write_text(
        json.dumps({"version": STATE_VERSION, "season_id": "other"}), encoding="utf-8"
    )
    assert load_collection("123", tmp_path, tmp_path).episodes == {}


def test_load_unknown_version_returns_empty(tmp_path):
    path = collection_state_path("123", tmp_path, tmp_path)
    path.write_text(
        json.dumps({"version": 999, "season_id": "123"}), encoding="utf-8"
    )
    assert load_collection("123", tmp_path, tmp_path).episodes == {}


# ---- 查询 ----


def test_status_of_recorded_and_unknown(tmp_path):
    state = CollectionState(season_id="123")
    record_episode(state, _episode(bvid="BV1", index=1), EpisodeStatus.NO_SUBTITLE)
    assert status_of(state, _episode(bvid="BV1", index=1)) == EpisodeStatus.NO_SUBTITLE
    assert status_of(state, _episode(bvid="BV9", index=9)) is None
