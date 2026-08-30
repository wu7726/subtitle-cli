"""vault 层：Obsidian vault 配置持久化、路径拼装与三态检查（PRD F1/F5）。

无网络。配置文件只存 vault 路径与字幕文件夹，绝不存 Cookie 等敏感信息
（PRD §7）。检查结果三态：✅ 可写且是 vault 根 / ⚠️ 可写但非根 / ❌ 不可用。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

DEFAULT_SUBDIR = "B站字幕"
_OBSIDIAN_DIR = ".obsidian"
_PROBE_NAME = ".subtitle-cli-write-probe"


class VaultConfig(BaseModel):
    """本地配置（~/.subtitle-cli/config.json）。"""

    vault: str = ""
    subdir: str = DEFAULT_SUBDIR


class VaultCheckStatus(BaseModel):
    """检查 vault 的结果；ok 等价于可写入。

    is_vault_root=False（未找到 .obsidian）仍可写入，但 obsidian://
    打开链接不可用（开发计划 §0 / 设计稿 §2）。
    """

    ok: bool
    writable: bool
    is_vault_root: bool = False
    can_create: bool = False
    message: str = ""


def config_path() -> Path:
    return Path.home() / ".subtitle-cli" / "config.json"


def load_config(path: Path | None = None) -> VaultConfig:
    """读取配置；文件缺失/损坏/字段非法一律回退默认值，不抛。"""
    target = Path(path) if path else config_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return VaultConfig.model_validate(data)
    except (OSError, ValueError):
        return VaultConfig()


def save_config(cfg: VaultConfig, path: Path | None = None) -> None:
    """原子写入（临时文件 + replace），进程中断不会留下半截配置。"""
    target = Path(path) if path else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(target)


def collection_root(cfg: VaultConfig) -> Path:
    """笔记落点：<vault>/<subdir>（subdir 支持嵌套，如 学习/B站字幕）。"""
    vault = cfg.vault.strip()
    if not vault:
        raise ValueError(
            "尚未配置 vault 路径：请先用 --vault 指定一次（会记住），"
            "或改用 --output 输出到普通文件夹。"
        )
    sub = cfg.subdir.strip()
    return Path(vault).expanduser() / (sub or ".")


def _probe_writable(directory: Path) -> bool:
    try:
        probe = directory / _PROBE_NAME
        with open(probe, "x", encoding="utf-8") as f:
            f.write("probe")
        probe.unlink()
        return True
    except OSError:
        return False


def check_vault(path: str, *, create: bool = False) -> VaultCheckStatus:
    """三态检查。路径不存在且 create=True 时先创建目录再复查。"""
    trimmed = (path or "").strip()
    if not trimmed:
        return VaultCheckStatus(ok=False, writable=False, message="路径为空")

    target = Path(trimmed).expanduser()
    if not target.exists():
        if create:
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return VaultCheckStatus(
                    ok=False,
                    writable=False,
                    can_create=True,
                    message=f"创建失败：{exc}",
                )
        else:
            return VaultCheckStatus(
                ok=False,
                writable=False,
                can_create=True,
                message="路径不存在（可用「创建该文件夹」后重查）",
            )
    if not target.is_dir():
        return VaultCheckStatus(
            ok=False, writable=False, message="路径不是文件夹"
        )
    if not _probe_writable(target):
        return VaultCheckStatus(
            ok=False, writable=False, message="路径不可写（权限不足或被占用）"
        )

    if (target / _OBSIDIAN_DIR).is_dir():
        return VaultCheckStatus(
            ok=True,
            writable=True,
            is_vault_root=True,
            message="可写，有效的 vault 根目录",
        )
    return VaultCheckStatus(
        ok=True,
        writable=True,
        is_vault_root=False,
        message="可写，但未找到 .obsidian——可能是 vault 子目录；"
        "仍可写入，但「在 Obsidian 中打开」链接不可用",
    )
