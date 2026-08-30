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

from .bilibili.client import BilibiliClient, BilibiliError, RiskControlError
from .pipeline import has_failure, run_collection, summarize

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
    output: Path = typer.Option(
        Path("."),
        "--output",
        "-o",
        help="下载根目录（默认当前目录），实际输出到 <output>/<合集名>/EP01 xx.md",
    ),
    cookie: Optional[str] = typer.Option(
        None,
        "--cookie",
        help="B站 Cookie（至少含 SESSDATA，AI 字幕需要登录态）；也可用 BILI_COOKIE 环境变量",
    ),
) -> None:
    """提取B站合集全部分集的字幕，保存为 Markdown 文件。"""
    _force_utf8_stdio()
    cookie = cookie or os.environ.get("BILI_COOKIE") or None
    if cookie and "sessdata" not in cookie.lower():
        typer.echo(
            "Cookie 中没有发现 SESSDATA 字段，无法获取字幕列表。请从浏览器开发者"
            "工具复制完整 Cookie 整串（至少包含 SESSDATA=...）。",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        typer.echo(f"输出目录不可用：{output}（{exc}）", err=True)
        raise typer.Exit(code=2) from None

    try:
        with BilibiliClient(cookie=cookie) as client:
            outcome = run_collection(source, output, client, log=typer.echo)
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
