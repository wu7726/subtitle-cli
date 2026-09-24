"""运行状态记录：单集稳定键与按合集落盘的状态文件（优化方案 刀1）。

状态目录默认 ~/.subtitle-cli/runs/，每个合集一个 JSON 文件。写入走
「临时文件 + replace」原子替换（同 vault.save_config），进程中断不会
留下半截状态。这里记录的是"这一集查过了/做过了"的**结论**（成功、
无字幕、失败带原因），供增量重跑与 subtitle-cli-status 查询使用。

绝不写进 vault、不碰产物 Markdown：历史产物没有属性头，也不该为
状态记录改动任何笔记格式。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .bilibili.models import Episode, EpisodeStatus
from .storage import sanitize_filename

# 播客 feed 缺单集 key 时 bvid 退化成位置序号 podcast-{index}
# （podcast/client.py），feed 顺序一变键就漂移。记录 key_positional
# 让查询端能提示"这条记录的键不可靠"。
_POSITIONAL_PREFIX = "podcast-"

STATE_VERSION = 1


def episode_key(episode: Episode) -> str:
    """单集稳定键：cid 优先，回落 bvid。

    B站多P 的所有分P 共用同一个 bvid，只有 cid 唯一，必须靠 cid 区分；
    合集/抖音的 bvid 本来就唯一（cid 在列表阶段为 None，自然回落）。
    """
    return episode.cid or episode.bvid


def key_is_positional(episode: Episode) -> bool:
    """键是否为位置序号兜底（播客 feed 无单集 key 时），不可靠。"""
    return episode.cid is None and episode.bvid.startswith(_POSITIONAL_PREFIX)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


class EpisodeState(BaseModel):
    """一集的结论记录（键就是 episodes 字典的键，不重复存一份）。"""

    index: int
    title: str
    status: EpisodeStatus
    reason: str | None = None
    key_positional: bool = False  # 键为位置序号兜底时为 True，不可靠、不参与跳过
    updated: str = ""


class CollectionState(BaseModel):
    """一个合集在**某个落点**的状态文件内容（按集键索引）。"""

    version: int = STATE_VERSION
    season_id: str
    output_dir: str = ""  # 本次落点（绝对路径，供 status 展示与人工核对）
    collection_name: str = ""  # 最近一次见到的合集名，便于 status 展示
    updated: str = ""
    episodes: dict[str, EpisodeState] = Field(default_factory=dict)


def state_root(path: Path | None = None) -> Path:
    """状态目录；SUBTITLE_CLI_STATE_DIR 可覆盖（测试/多实例用）。"""
    if path is not None:
        return path
    override = os.environ.get("SUBTITLE_CLI_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".subtitle-cli" / "runs"


def collection_state_path(
    season_id: str,
    output_dir: Path | str = "",
    root: Path | None = None,
) -> Path:
    """某合集在**某落点**的状态文件路径：<root>/<season 清洗>-<落点指纹>.json。

    落点也进文件名：同一合集换个输出目录再跑，是要在新地方重新出一份，不能因为
    "这个合集做过"就跳过，否则新目录永远是空的。所以先绝对化 + 大小写折叠 +
    统一斜杠再取 8 位哈希（同一落点的不同写法必须指同一个文件）。
    season_id 来自各平台 resolve_input（数字 sid / BV 号 / feed 地址），含 URL
    特殊字符，统一走文件名清洗；完整值与落点存在文件内容里，load 时核对防碰撞。
    """
    stem = sanitize_filename(season_id)[:40] or "_"
    if output_dir and str(output_dir).strip():
        target = str(Path(output_dir).resolve()).casefold().replace("\\", "/")
        stem = f"{stem}-{_short_hash(target)}"
    return state_root(root) / f"{stem}.json"


def audio_cache_root(path: Path | None = None) -> Path:
    """音频缓存目录；SUBTITLE_CLI_AUDIO_CACHE_DIR 可覆盖（测试/多实例用）。"""
    if path is not None:
        return path
    override = os.environ.get("SUBTITLE_CLI_AUDIO_CACHE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".subtitle-cli" / "cache"


def audio_cache_path(episode: Episode, root: Path | None = None) -> Path:
    """某集的音频缓存文件：<root>/<集键清洗-短哈希>.bin。

    键可能很长（播客 guid 是 URL），故清洗后截断并补哈希防碰撞。
    后缀无所谓：PyAV 按内容探测容器（与原先的 audio-N.bin 一致）。
    只有**转写失败**的集才会留在这里，重跑直接复用，不必重新下载。
    """
    key = episode_key(episode)
    stem = sanitize_filename(key)[:40] or "_"
    return audio_cache_root(root) / f"{stem}-{_short_hash(key)}.bin"


def _read_state_file(path: Path) -> CollectionState | None:
    """读一个状态文件；缺失 / 损坏 / 版本不识别返回 None。"""
    try:
        parsed = CollectionState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    return parsed if parsed.version == STATE_VERSION else None


def load_collection(
    season_id: str,
    output_dir: Path | str = "",
    root: Path | None = None,
) -> CollectionState:
    """读取合集在指定落点的状态。

    缺失/损坏/版本不识别/season_id 对不上一律回退空状态——宁可当没有记录
    （多花一次网络请求），也不拿错误记录去误跳过。
    """
    loaded = _read_state_file(collection_state_path(season_id, output_dir, root))
    if loaded is None or loaded.season_id != season_id:
        return CollectionState(season_id=season_id, output_dir=_display_dir(output_dir))
    return loaded


def iter_collections(root: Path | None = None) -> list[CollectionState]:
    """状态目录下的全部合集记录，按最近更新倒序（供 subtitle-cli-status）。"""
    directory = state_root(root)
    if not directory.is_dir():
        return []
    states = (s for s in (_read_state_file(p) for p in directory.glob("*.json")) if s)
    return sorted(states, key=lambda s: s.updated, reverse=True)


def _display_dir(output_dir: Path | str) -> str:
    return str(Path(output_dir).resolve()) if str(output_dir).strip() else ""


def save_collection(state: CollectionState, root: Path | None = None) -> Path:
    """原子写入状态文件（临时文件 + replace），返回落点路径。"""
    state.updated = _now()
    target = collection_state_path(state.season_id, state.output_dir, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def record_episode(
    state: CollectionState,
    episode: Episode,
    status: EpisodeStatus,
    reason: str | None = None,
) -> CollectionState:
    """登记一集的结论（只改内存；落盘时机由调用方 save_collection 决定）。"""
    key = episode_key(episode)
    state.episodes[key] = EpisodeState(
        index=episode.index,
        title=episode.title,
        status=status,
        reason=reason,
        key_positional=key_is_positional(episode),
        updated=_now(),
    )
    return state


def status_of(state: CollectionState, episode: Episode) -> EpisodeStatus | None:
    """该集已记录的结论；没有记录返回 None。"""
    recorded = state.episodes.get(episode_key(episode))
    return recorded.status if recorded else None
