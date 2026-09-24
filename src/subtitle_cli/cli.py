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

from . import config
from .asr import AsrDependencyError
from .bilibili.client import RiskControlError, normalize_cookie
from .dispatch import BILIBILI, DOUYIN, PODCAST, create_client, detect_platform
from .errors import PlatformError
from .pipeline import format_preview, has_failure, preview_first_episode, run_collection, summarize
from .vault import collection_root, load_config, save_config

app = typer.Typer(add_completion=False, help="字幕提取器：B站合集与播客 → Obsidian 笔记。")


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
        help="B站：合集页 URL（含 sid= 或 season_id=）、合集内任一视频的 URL 或 BV 号、纯数字 season_id；"
        "播客：RSS 地址或 Apple Podcasts 节目链接；抖音：分享口令（含 v.douyin.com 短链）或视频页链接",
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
        help="vault 内字幕文件夹（默认 B站字幕，可嵌套；播客默认 播客字幕，抖音默认 抖音字幕）",
    ),
    cookie: Optional[str] = typer.Option(
        None,
        "--cookie",
        help="B站 Cookie（至少含 SESSDATA，AI 字幕需要登录态）；也可用 BILI_COOKIE 环境变量。播客输入不需要 Cookie",
    ),
    preview: bool = typer.Option(
        False,
        "--preview",
        help="只提取第 1 集并输出审查报告（排版与内容清洗情况），不写文件",
    ),
    asr: bool = typer.Option(
        False,
        "--asr",
        help="无字幕/无文稿的分集下载音频，用本地语音转写兜底（需先 pip install -e \".[asr]\"；"
        "首次运行自动下载模型；有 NVIDIA 显卡并装好 cuBLAS/cuDNN 时自动用显卡转写；"
        "重跑会自动跳过已成功分集）",
    ),
    asr_model: str = typer.Option(
        config.ASR_MODEL_SIZE,
        "--asr-model",
        help="语音转写模型：tiny/base/small/medium（越大越准越慢，默认 medium；"
        "回退到 CPU 时建议改 small）",
    ),
    asr_limit: Optional[int] = typer.Option(
        None,
        "--asr-limit",
        help="本次最多转写多少集（默认不限；无字幕分集很多时建议限制）",
    ),
    recheck: bool = typer.Option(
        False,
        "--recheck",
        help="重跑时也重新联网检查上次确认「无字幕」的分集（默认跳过；UP主后补字幕时用）",
    ),
    no_audio_cache: bool = typer.Option(
        False,
        "--no-audio-cache",
        help="转写不复用音频缓存，每次都重新下载（默认：转写成功即删、失败的留着供重跑直接转写）",
    ),
) -> None:
    """提取B站合集或播客的字幕，保存为 Markdown 文件。"""
    _force_utf8_stdio()
    platform = detect_platform(source)
    cookie = cookie or os.environ.get("BILI_COOKIE") or None
    if cookie and platform == BILIBILI:
        cookie, cookie_note = normalize_cookie(cookie)
        if cookie_note:
            typer.echo(cookie_note)
        if "sessdata" not in cookie.lower():
            typer.echo("无法获取字幕列表，已停止。", err=True)
            raise typer.Exit(code=2)

    # 输出模式判定（开发计划 M7 + 播客分目录）：--output 显式给出 → 普通文件夹
    # 优先；否则已配置 vault（参数 > 配置文件，显式传入即写回）→ obsidian 模式；
    # 都没有 → 沿用旧默认（当前目录，普通输出）。播客落 podcast_subdir。
    if vault or vault_subdir:
        cfg = load_config()
        if vault:
            cfg.vault = vault
        if vault_subdir:
            if platform == PODCAST:
                cfg.podcast_subdir = vault_subdir
            elif platform == DOUYIN:
                cfg.douyin_subdir = vault_subdir
            else:
                cfg.subdir = vault_subdir
        save_config(cfg)
    cfg = load_config()
    if output is None and cfg.vault.strip():
        note_mode = "obsidian"
        default_subdir = (
            cfg.podcast_subdir
            if platform == PODCAST
            else cfg.douyin_subdir
            if platform == DOUYIN
            else cfg.subdir
        )
        output = collection_root(cfg, vault_subdir or default_subdir)
    else:
        note_mode = "plain"
        output = output if output is not None else Path(".")

    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        typer.echo(f"输出目录不可用：{output}（{exc}）", err=True)
        raise typer.Exit(code=2) from None

    try:
        with create_client(source, cookie) as client:
            if preview:
                result = preview_first_episode(
                    source, client, log=typer.echo, note_mode=note_mode
                )
                typer.echo(format_preview(result))
                raise typer.Exit(code=0)
            outcome = run_collection(
                source, output, client, log=typer.echo, note_mode=note_mode,
                asr=asr, asr_model=asr_model, asr_limit=asr_limit, recheck=recheck,
                audio_cache=not no_audio_cache,
            )
    except ValueError as exc:
        typer.echo(f"输入无效：{exc}", err=True)
        raise typer.Exit(code=2) from None
    except AsrDependencyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    except RiskControlError as exc:
        typer.echo(f"触发风控，已停止：{exc}\n稍后重跑同一条命令，已成功的分集会自动跳过。", err=True)
        raise typer.Exit(code=1) from None
    except PlatformError as exc:
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
