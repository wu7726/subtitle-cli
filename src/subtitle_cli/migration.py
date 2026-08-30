"""旧字幕离线迁移：旧格式 Markdown → vault 属性头笔记（PRD F4）。

无网络、无 Cookie：本模块不 import bilibili / pipeline（开发计划 §0，
由离线性测试锁死）。恢复不了的字段（bvid、url）写空串并在结果中说明
（开发计划 §2.4）。覆盖规则（PRD §5.2 / §7）：
- 目标已有本工具迁移产物（fetched_by 标记）→ 默认跳过，--overwrite 重写；
- 目标是提取产物（无标记）→ 永远跳过：提取产物信息更完整，不得被迁移覆盖。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from . import notes, storage
from .vault import VaultConfig, collection_root

_EP_FILENAME = re.compile(r"EP(\d+)\s+(.+)")


class LegacyNote(BaseModel):
    """一份旧格式笔记的解析结果。"""

    index: int
    filename_title: str
    heading: str | None  # 文件内一级标题（优先用作属性头 title）
    text: str  # 原文全文（\n 归一化，结尾恰一个换行）——正文一字不动


class CollectionScan(BaseModel):
    name: str
    path: str
    files: list[str]
    pending: list[str]
    already: list[str]
    unparsable: list[str]


class MigrationScan(BaseModel):
    root: str
    collections: list[CollectionScan]
    loose_files: list[str]  # 根目录散落的 .md（不在合集子文件夹内，不迁移）


class FileOutcome(BaseModel):
    source: str
    target: str = ""
    status: Literal["migrated", "skipped", "failed"]
    reason: str | None = None
    missing_fields: list[str] = []


class CollectionResult(BaseModel):
    name: str
    target_dir: str
    files: list[FileOutcome]
    index_path: str | None = None
    index_episodes: int = 0


class MigrationOutcome(BaseModel):
    results: list[CollectionResult]
    loose_files: list[str] = []
    dry_run: bool = False
    vault_dir: str = ""


def _parse_note_text(text: str, stem: str) -> LegacyNote | None:
    match = _EP_FILENAME.fullmatch(stem)
    if not match:
        return None
    text = text.replace("\r\n", "\n")
    heading = None
    for line in text.splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            break
    if text and not text.endswith("\n"):
        text += "\n"
    return LegacyNote(
        index=int(match.group(1)),
        filename_title=match.group(2).strip(),
        heading=heading,
        text=text,
    )


def parse_legacy_note(path: Path) -> LegacyNote | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return _parse_note_text(text, path.stem)


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def scan_collections(root: Path) -> MigrationScan:
    """扫描旧目录：含 .md 的子文件夹视作一个合集，并按「待迁移/已迁移/无法
    解析」预分类。根目录散落的 .md 只报告不处理。"""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"旧字幕目录不存在：{root}")
    scans: list[CollectionScan] = []
    loose: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_file():
            continue
        if child.suffix.lower() == ".md":
            loose.append(str(child))
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        mds = [p for p in sorted(child.glob("*.md")) if p.is_file()]
        if not mds:
            continue
        pending: list[str] = []
        already: list[str] = []
        unparsable: list[str] = []
        for md in mds:
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                unparsable.append(str(md))
                continue
            if notes.has_migration_marker(text):
                already.append(str(md))
            elif _parse_note_text(text, md.stem) is None:
                unparsable.append(str(md))
            else:
                pending.append(str(md))
        scans.append(
            CollectionScan(
                name=child.name,
                path=str(child),
                files=[str(p) for p in mds],
                pending=pending,
                already=already,
                unparsable=unparsable,
            )
        )
    return MigrationScan(root=str(root), collections=scans, loose_files=loose)


def migrate(
    source_root: Path,
    cfg: VaultConfig,
    collections: list[str] | None = None,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    fetched_at: date | None = None,
    log: Callable[[str], None] = print,
) -> MigrationOutcome:
    """执行迁移。collections 为 None 迁移全部；dry_run 只算不写。

    输出目录：<vault>/<subdir>/<合集名>/（与提取落点一致，PRD §6）。
    """
    fetched = fetched_at or date.today()
    scan = scan_collections(source_root)
    target_root = collection_root(cfg)  # vault 未配置 → ValueError（上层转退出码 2）
    selected = [
        c for c in scan.collections if collections is None or c.name in collections
    ]
    unknown = [
        name for name in (collections or []) if name not in {c.name for c in scan.collections}
    ]
    results = [
        _migrate_collection(
            c,
            target_root / storage.collection_dirname(c.name),
            overwrite=overwrite,
            dry_run=dry_run,
            fetched_at=fetched,
            log=log,
        )
        for c in selected
    ]
    for name in unknown:
        log(f"未找到合集「{name}」，已跳过")
    return MigrationOutcome(
        results=results,
        loose_files=scan.loose_files,
        dry_run=dry_run,
        vault_dir=str(target_root),
    )


def _migrate_collection(
    scan: CollectionScan,
    target_dir: Path,
    *,
    overwrite: bool,
    dry_run: bool,
    fetched_at: date,
    log: Callable[[str], None],
) -> CollectionResult:
    log(
        f"迁移《{scan.name}》：待迁移 {len(scan.pending)}、已迁移 {len(scan.already)}、"
        f"无法解析 {len(scan.unparsable)} → {target_dir}"
    )
    outcomes: list[FileOutcome] = []
    for source in scan.files:
        note = parse_legacy_note(Path(source))
        if note is None:
            outcomes.append(
                FileOutcome(
                    source=source,
                    status="failed",
                    reason="文件名或内容不符合 EP NN 标题 格式，或编码错误",
                )
            )
            continue
        target = target_dir / storage.episode_filename(note.index, note.filename_title)
        if target.exists():
            if notes.has_migration_marker(_safe_read(target)):
                if not overwrite:
                    outcomes.append(
                        FileOutcome(source=source, target=str(target), status="skipped",
                                    reason="已迁移过")
                    )
                    continue
            else:
                # 提取产物（有真实 bvid），不得被离线迁移覆盖（PRD §7 纪律）
                outcomes.append(
                    FileOutcome(source=source, target=str(target), status="skipped",
                                reason="目标已存在（提取产物，不覆盖）")
                )
                continue
        title = note.heading or f"第{note.index}集 {note.filename_title}"
        meta = notes.EpisodeMeta(
            title=title,
            collection=scan.name,
            season_id=None,
            bvid="",
            episode_index=note.index,
            is_multi_p=False,
            fetched_at=fetched_at,
        )
        content = notes.build_episode_note(meta, note.text)
        if not dry_run:
            try:
                storage.write_markdown(
                    target, content, overwrite=overwrite and target.exists()
                )
            except OSError as exc:
                outcomes.append(
                    FileOutcome(source=source, target=str(target), status="failed",
                                reason=f"写入失败：{exc}")
                )
                continue
        outcomes.append(
            FileOutcome(source=source, target=str(target), status="migrated",
                        missing_fields=["bvid", "url"])
        )

    index_path = None
    index_episodes = 0
    if not dry_run and target_dir.is_dir():
        index_path = storage.write_collection_index(
            target_dir, scan.name, None, fetched_at, log
        )
        index_episodes = sum(
            1
            for md in target_dir.glob("EP*.md")
            if md.stem != storage.collection_dirname(scan.name)
        )
    return CollectionResult(
        name=scan.name,
        target_dir=str(target_dir),
        files=outcomes,
        index_path=str(index_path) if index_path else None,
        index_episodes=index_episodes,
    )


def format_migration_summary(outcome: MigrationOutcome) -> str:
    files = [f for r in outcome.results for f in r.files]
    migrated = [f for f in files if f.status == "migrated"]
    skipped = [f for f in files if f.status == "skipped"]
    failed = [f for f in files if f.status == "failed"]
    header = "—— 迁移汇总 ——" + ("（演练，未写盘）" if outcome.dry_run else "")
    lines = [header]
    lines.append(f"成功 {len(migrated) + len(skipped)}（迁移 {len(migrated)}、跳过 {len(skipped)}）")
    if failed:
        labels = "、".join(
            f"{Path(f.source).name}（{f.reason}）" if f.reason else Path(f.source).name
            for f in failed
        )
        lines.append(f"失败 {len(failed)}：{labels}")
    if migrated:
        lines.append("缺失字段：bvid、url（离线迁移无法恢复，已留空）")
    if outcome.loose_files:
        lines.append(f"未处理的散落文件 {len(outcome.loose_files)} 个（不在合集子文件夹内）")
    if outcome.vault_dir:
        lines.append(f"目标：{outcome.vault_dir}")
    return "\n".join(lines)
