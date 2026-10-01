"""任务线程：提取与迁移的后台执行体，以及演示 Mock 的启停。

任务只通过 state 模块改写共享状态；完成时把产物文件列表与
obsidian:// 打开链接一并写入，供轮询端点与前端展示。
"""

from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import quote

from subtitle_cli import storage
from subtitle_cli.bilibili.models import EpisodeStatus
from subtitle_cli.config import ASR_MODEL_SIZE
from subtitle_cli.dispatch import BILIBILI, create_client, detect_platform
from subtitle_cli.migration import format_migration_summary, migrate
from subtitle_cli.pipeline import has_failure, run_collection, summarize
from subtitle_cli.vault import check_vault, load_config

from .httpd import LocalServer
from .state import STATE, file_log, finish_error, log_line

_demo_stops: list[callable] = []


def _obsidian_uri(vault_path: str, rel_path: str) -> str | None:
    """obsidian:// 打开链接；仅 vault 根目录有效时可用（设计稿 §3.3）。"""
    status = check_vault(vault_path)
    if not status.ok or not status.is_vault_root:
        return None
    vault_name = quote(Path(vault_path).expanduser().name)
    return f"obsidian://open?vault={vault_name}&file={quote(rel_path)}"


def _badge_files_extract(outcome, output_dir: Path) -> list[dict]:
    """提取产物文件列表：索引页置顶，逐个带徽标（新写入/跳过/已有）。"""
    ep_dir = Path(output_dir) / storage.collection_dirname(outcome.collection_name)
    badge_by_name: dict[str, str] = {}
    for r in outcome.results:
        name = storage.output_path(
            output_dir, outcome.collection_name, r.episode.index, r.episode.title
        ).name
        if r.status == EpisodeStatus.SUCCESS:
            badge_by_name[name] = "新写入"
        elif r.status == EpisodeStatus.SKIPPED:
            badge_by_name[name] = "跳过"
    index_stem = storage.collection_dirname(outcome.collection_name)
    files: list[dict] = []
    index_file = ep_dir / f"{index_stem}.md"
    if outcome.note_mode == "obsidian" and index_file.is_file():
        files.append({"name": index_file.name, "badge": "索引"})
    if ep_dir.is_dir():
        for p in sorted(ep_dir.glob("*.md")):
            if p.name == index_file.name:
                continue
            files.append({"name": p.name, "badge": badge_by_name.get(p.name, "已有")})
    return files


def _vault_rel_index_path(subdir: str, collection_name: str) -> str:
    coll = storage.collection_dirname(collection_name)
    sub = Path(subdir.strip() or ".")
    return (sub / coll / coll).as_posix()


def start_demo() -> str:
    """启动本地 Mock 并把接口层指过去，返回其 base URL（恢复用 stop_demo）。"""
    from demo.run_demo import make_handler, patch_client_to

    server = LocalServer(("127.0.0.1", 0), make_handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    restore_urls = patch_client_to(base, speedup=True)

    def stop() -> None:
        restore_urls()
        server.shutdown()
        server.server_close()

    _demo_stops.append(stop)
    return base


def stop_demo() -> None:
    while _demo_stops:
        stop = _demo_stops.pop(0)
        try:
            stop()
        except Exception:  # noqa: BLE001
            pass


def run_extract_job(
    source: str,
    cookie: str | None,
    demo: bool,
    output_dir: str,
    *,
    note_mode: str = "plain",
    vault_path: str = "",
    vault_subdir: str = "",
    asr: bool = False,
    asr_model: str = ASR_MODEL_SIZE,
    asr_limit: int | None = None,
    on_progress=None,
    proxy: str | None = None,
) -> None:
    try:
        platform = detect_platform(source)
        file_log.info(
            "提取开始：platform=%s demo=%s 落点=%s", platform, demo, output_dir
        )
        if demo and platform != BILIBILI:
            finish_error("演示模式使用内置B站示例数据，仅支持B站输入。", 2)
            return
        if demo and asr:
            log_line("演示模式不支持语音转写，本次按普通提取执行")
            asr = False
        if demo:
            # 演示模式同样支持粘贴视频链接（Mock 的 view 路由会反查出演示合集）；
            # 输入为空时使用默认演示合集链接
            from demo.run_demo import DEMO_SOURCE

            start_demo()
            source = (source or "").strip() or DEMO_SOURCE
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        with create_client(source, cookie or None, proxy=proxy) as client:
            outcome = run_collection(
                source, Path(output_dir), client, log=log_line, note_mode=note_mode,
                asr=asr, asr_model=asr_model, asr_limit=asr_limit,
                on_progress=on_progress, proxy=proxy,
            )
        STATE["summary"] = summarize(outcome)
        STATE["exit_code"] = 1 if has_failure(outcome) else 0
        STATE["collection_name"] = outcome.collection_name
        STATE["files"] = _badge_files_extract(outcome, Path(output_dir))
        file_log.info("提取结束：%s", STATE["summary"])
        if note_mode == "obsidian" and vault_path:
            subdir = vault_subdir or load_config().subdir
            STATE["obsidian_open"] = _obsidian_uri(
                vault_path, _vault_rel_index_path(subdir, outcome.collection_name)
            )
        STATE["phase"] = "done"
    except ValueError as exc:
        file_log.error("提取输入无效：%s", exc)
        finish_error(str(exc), 2)
    except Exception as exc:  # noqa: BLE001 - 网页界面兜底展示；traceback 进文件日志
        file_log.exception("提取任务异常")
        finish_error(f"{type(exc).__name__}: {exc}")
    finally:
        if demo:
            stop_demo()
        STATE["running"] = False


def run_migrate_job(
    source_dir: str, collections: list[str] | None, overwrite: bool
) -> None:
    try:
        file_log.info("迁移开始：source=%s overwrite=%s", source_dir, overwrite)
        outcome = migrate(
            Path(source_dir),
            load_config(),
            collections,
            overwrite=overwrite,
            log=log_line,
        )
        STATE["summary"] = format_migration_summary(outcome)
        failed = any(
            f.status == "failed" for r in outcome.results for f in r.files
        )
        STATE["exit_code"] = 1 if failed else 0
        STATE["files"] = []
        file_log.info("迁移结束：%s", STATE["summary"])
        for r in outcome.results:
            if r.index_path:
                STATE["files"].append({"name": Path(r.index_path).name, "badge": "索引"})
            for f in r.files:
                if f.status == "failed":
                    STATE["files"].append({"name": Path(f.source).name, "badge": "失败"})
                elif f.target:
                    STATE["files"].append(
                        {
                            "name": Path(f.target).name,
                            "badge": "迁移" if f.status == "migrated" else "跳过",
                        }
                    )
        if outcome.results:
            first = outcome.results[0]
            STATE["collection_name"] = first.name
            STATE["output_dir"] = str(Path(first.target_dir).parent)
            if first.index_path and outcome.vault_dir:
                cfg = load_config()
                index_rel = Path(first.index_path).relative_to(
                    Path(cfg.vault).expanduser()
                )
                STATE["obsidian_open"] = _obsidian_uri(
                    cfg.vault, index_rel.with_suffix("").as_posix()
                )
        STATE["phase"] = "done"
    except ValueError as exc:
        file_log.error("迁移输入无效：%s", exc)
        finish_error(str(exc), 2)
    except Exception as exc:  # noqa: BLE001 - 网页界面兜底展示；traceback 进文件日志
        file_log.exception("迁移任务异常")
        finish_error(f"{type(exc).__name__}: {exc}")
    finally:
        STATE["running"] = False
