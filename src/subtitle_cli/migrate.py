"""subtitle-cli-migrate 入口：旧字幕目录 → Obsidian vault（离线，PRD F4）。

独立于 subtitle-cli 主命令，避免破坏 `subtitle-cli <来源>` 的现有用法
（开发计划 M8）。退出码：0 无失败；1 存在失败；2 参数/输入错误。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from .migration import format_migration_summary, migrate
from .vault import load_config, save_config

app = typer.Typer(add_completion=False, help="把已下载的旧字幕目录迁移为 Obsidian vault 笔记（不联网、不需要 Cookie）。")


def _force_utf8_stdio() -> None:
    """Windows 下重定向输出时默认用本地编码，统一改为 UTF-8 防乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


@app.command()
def main(
    source_dir: Path = typer.Argument(
        ...,
        help="旧字幕根目录：其下每个含 .md 的子文件夹视作一个合集",
    ),
    vault: Optional[str] = typer.Option(
        None,
        "--vault",
        help="Obsidian vault 根目录（传入即记住）",
    ),
    vault_subdir: Optional[str] = typer.Option(
        None,
        "--vault-subdir",
        help="vault 内字幕文件夹（默认 B站字幕，可嵌套）",
    ),
    collections: Optional[str] = typer.Option(
        None,
        "--collections",
        help="逗号分隔的合集名，仅迁移这些；缺省迁移全部",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="重写已迁移过的笔记（提取产物仍不会被覆盖）",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="只打印迁移计划，不写任何文件",
    ),
) -> None:
    """扫描旧字幕目录，转换成带属性头的笔记并写入 vault，同时生成索引页。"""
    _force_utf8_stdio()
    if vault or vault_subdir:
        cfg = load_config()
        if vault:
            cfg.vault = vault
        if vault_subdir:
            cfg.subdir = vault_subdir
        save_config(cfg)
    cfg = load_config()
    names = None
    if collections:
        names = [s.strip() for s in collections.split(",") if s.strip()]
    try:
        outcome = migrate(
            source_dir,
            cfg,
            names,
            overwrite=overwrite,
            dry_run=dry_run,
            log=typer.echo,
        )
    except ValueError as exc:
        typer.echo(f"输入无效：{exc}", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(format_migration_summary(outcome))
    if any(f.status == "failed" for r in outcome.results for f in r.files):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
