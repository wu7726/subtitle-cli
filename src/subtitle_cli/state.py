"""运行状态记录：单集稳定键与按合集落盘的状态文件（优化方案 刀1）。

状态目录默认 ~/.subtitle-cli/runs/，每个合集一个 JSON 文件。写入走
「临时文件 + replace」原子替换（同 vault.save_config），进程中断不会
留下半截状态。这里记录的是"这一集查过了/做过了"的**结论**（成功、
无字幕、失败带原因），供增量重跑与 subtitle-cli-status 查询使用。

绝不写进 vault、不碰产物 Markdown：历史产物没有属性头，也不该为
状态记录改动任何笔记格式。
"""

from __future__ import annotations

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


class EpisodeState(BaseModel):
    """一集的结论记录。title/bvid 只是快照，供 status 展示与人工核对。"""

    key: str
    index: int
    bvid: str
    title: str
    status: EpisodeStatus
    reason: str | None = None
    key_positional: bool = False  # 键为位置序号兜底时为 True，提示不可靠
    updated: str = ""


class CollectionState(BaseModel):
    """一个合集的状态文件内容（按集键索引）。"""

    version: int = STATE_VERSION
    season_id: str
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


def collection_state_path(season_id: str, root: Path | None = None) -> Path:
    """某合集的状态文件路径：<root>/<season_id 清洗截断>.json。

    season_id 来自各平台 resolve_input（数字 sid / BV 号 / feed 地址）；
    feed 地址含 URL 特殊字符，统一走文件名清洗并截断至 60 字符。
    完整 season_id 存在文件内容里，load 时核对以防截断碰撞。
    """
    stem = sanitize_filename(season_id)[:60]
    if not stem:
        stem = "_"
    return state_root(root) / f"{stem}.json"


def load_collection(season_id: str, root: Path | None = None) -> CollectionState:
    """读取合集状态；缺失/损坏/版本不识别/season_id 对不上一律回退空状态。

    宁可当没有记录（多花一次网络请求），也不拿错误记录去误跳过。
    """
    path = collection_state_path(season_id, root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = CollectionState.model_validate(data)
    except (OSError, ValueError):
        return CollectionState(season_id=season_id)
    if state.version != STATE_VERSION or state.season_id != season_id:
        return CollectionState(season_id=season_id)
    return state


def save_collection(state: CollectionState, root: Path | None = None) -> Path:
    """原子写入状态文件（临时文件 + replace），返回落点路径。"""
    state.updated = _now()
    target = collection_state_path(state.season_id, root)
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
        key=key,
        index=episode.index,
        bvid=episode.bvid,
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
