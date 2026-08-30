"""CLI 入口（技术方案 §3 cli.py）：参数解析与退出码。

退出码：0 无失败；1 存在失败；2 参数/输入错误。
Cookie 属敏感凭据：只经参数或 BILI_COOKIE 环境变量传入，不写日志、不落盘。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import typer

from .bilibili.client import BilibiliClient, BilibiliError, RiskControlError, normalize_cookie
from .pipeline import format_preview, has_failure, preview_first_episode, run_collection, summarize
from .vault import collection_root, load_config, save_config

app = typer.Typer(add_completion=False, help="B站合集字幕提取器：输入合集或其内任一视频链接，一次性提取整个合集的字幕为 Markdown。")


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
    source: str = typer.Argument(
        ...,
        help="合集页 URL（含 sid= 或 season_id=）、合集内任一视频的 URL 或 BV 号（自动识别所属合集）、或纯数字 season_id",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="输出到普通文件夹（默认当前目录）；显式指定时优先于 vault",
    ),
    vault: Optional[str] = typer.Option(
        None,
        "--vault",
        help="Obsidian vault 根目录：笔记写入 <vault>/<字幕文件夹>/<合集名>/；"
        "传入即记住（下次可省略）",
    ),
    vault_subdir: Optional[str] = typer.Option(
        None,
        "--vault-subdir",
        help="vault 内字幕文件夹（默认 B站字幕，可嵌套如 学习/B站字幕）",
    ),
    cookie: Optional[str] = typer.Option(
        None,
        "--cookie",
        help="B站 Cookie（至少含 SESSDATA，AI 字幕需要登录态）；也可用 BILI_COOKIE 环境变量",
    ),
    preview: bool = typer.Option(
        False,
        "--preview",
        help="只提取第 1 集并输出审查报告（排版与内容清洗情况），不写文件",
    ),
) -> None:
    """提取B站合集全部分集的字幕，保存为 Markdown 文件。"""
    _force_utf8_stdio()
    cookie = cookie or os.environ.get("BILI_COOKIE") or None
    if cookie:
        cookie, cookie_note = normalize_cookie(cookie)
        if cookie_note:
            typer.echo(cookie_note)
        if "sessdata" not in cookie.lower():
            typer.echo("无法获取字幕列表，已停止。", err=True)
            raise typer.Exit(code=2)

    # 输出模式判定（开发计划 M7）：--output 显式给出 → 普通文件夹优先；
    # 否则已配置 vault（参数 > 配置文件，显式传入即写回）→ obsidian 模式；
    # 都没有 → 沿用旧默认（当前目录，普通输出）。
    if vault or vault_subdir:
        cfg = load_config()
        if vault:
            cfg.vault = vault
        if vault_subdir:
            cfg.subdir = vault_subdir
        save_config(cfg)
    cfg = load_config()
    if output is None and cfg.vault.strip():
        note_mode = "obsidian"
        output = collection_root(cfg)
    else:
        note_mode = "plain"
        output = output if output is not None else Path(".")

    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        typer.echo(f"输出目录不可用：{output}（{exc}）", err=True)
        raise typer.Exit(code=2) from None

    try:
        with BilibiliClient(cookie=cookie) as client:
            if preview:
                result = preview_first_episode(
                    source, client, log=typer.echo, note_mode=note_mode
                )
                typer.echo(format_preview(result))
                raise typer.Exit(code=0)
            outcome = run_collection(
                source, output, client, log=typer.echo, note_mode=note_mode
            )
    except ValueError as exc:
        typer.echo(f"输入无效：{exc}", err=True)
        raise typer.Exit(code=2) from None
    except RiskControlError as exc:
        typer.echo(f"触发风控，已停止：{exc}\n稍后重跑同一条命令，已成功的分集会自动跳过。", err=True)
        raise typer.Exit(code=1) from None
    except BilibiliError as exc:
        typer.echo(f"提取失败：{exc}", err=True)
        raise typer.Exit(code=1) from None
    except KeyboardInterrupt:
        typer.echo("\n已中断。已成功分集已落盘，重跑会自动跳过。", err=True)
        raise typer.Exit(code=130) from None

    typer.echo(summarize(outcome))
    if has_failure(outcome):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
