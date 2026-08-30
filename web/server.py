# -*- coding: utf-8 -*-
"""本地网页界面：在浏览器里运行字幕提取/迁移并查看生成的 Markdown。

    python web/server.py            # 默认 http://127.0.0.1:8765，自动打开浏览器
    python web/server.py --port 0   # 随机端口（打印 PORT=xxx 供测试读取）

两种模式：
- 离线演示：复用 demo/ 的本地 Mock（无需 Cookie、不访问真实网络）
- 真实接口：直连 api.bilibili.com，Cookie 只在本次运行中透传给 API 域，
  不写日志、不落盘（与 CLI 一致）

Obsidian vault（PRD F1-F6）：前端在真实模式下提供 vault 输出位置与迁移卡；
API 层对演示模式也接受 vault 参数，以便离线验证 vault 写入链路。

API：
    GET  /                  页面
    GET  /api/run           当前运行状态（轮询）
    GET  /api/file?name=    读取某集 Markdown（限制在本次输出目录内）
    POST /api/extract       {demo, source, cookie, output, vault, vault_subdir}
    POST /api/preview       提取前审查（vault 模式预览稿带属性头）
    POST /api/check-cookie  登录态检测
    GET/POST /api/config    vault 配置读写（~/.subtitle-cli/config.json）
    POST /api/check-vault   vault 三态检查（可带 create）
    POST /api/migrate-scan  扫描旧字幕目录 → 合集清单
    POST /api/migrate       后台线程执行迁移（复用 /api/run 轮询）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from subtitle_cli import storage  # noqa: E402
from subtitle_cli.bilibili.client import BilibiliClient, normalize_cookie  # noqa: E402
from subtitle_cli.bilibili.models import EpisodeStatus  # noqa: E402
from subtitle_cli.migration import (  # noqa: E402
    format_migration_summary,
    migrate,
    scan_collections,
)
from subtitle_cli.pipeline import (  # noqa: E402
    format_preview,
    has_failure,
    preview_first_episode,
    run_collection,
    summarize,
)
from subtitle_cli.vault import check_vault, collection_root, load_config, save_config  # noqa: E402

INDEX_HTML = REPO_ROOT / "web" / "index.html"

_lock = threading.Lock()


class LocalServer(ThreadingHTTPServer):
    """本地服务。Windows 下禁用 SO_REUSEADDR：默认行为允许第二个进程静默
    绑定同一端口，请求会被随机路由到（可能僵死的）旧实例，表现为页面能
    打开但接口时好时坏（Failed to fetch）。改为显式失败并提示。"""

    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def _fresh_state() -> dict:
    return {
        "running": False,
        "phase": "idle",  # idle | running | done | error
        "kind": "idle",  # idle | extract | migrate
        "demo": False,
        "source": "",
        "output_dir": "",
        "note_mode": "plain",
        "vault": "",
        "obsidian_open": None,  # obsidian:// 打开合集索引的 URI（不可用时 None）
        "log": [],  # 逐行进度（run_collection/migrate 的 log 回调输出）
        "summary": None,
        "exit_code": None,
        "collection_name": None,
        "files": [],  # [{name, badge}]，索引页固定第一
        "error": None,
    }


STATE: dict = _fresh_state()


def _log(line: str) -> None:
    STATE["log"].append({"line": line})


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


def _vault_rel_index_path(cfg, collection_name: str) -> str:
    coll = storage.collection_dirname(collection_name)
    sub = Path(cfg.subdir.strip() or ".")
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


_demo_stops: list[callable] = []


def stop_demo() -> None:
    while _demo_stops:
        stop = _demo_stops.pop(0)
        try:
            stop()
        except Exception:  # noqa: BLE001
            pass


def _finish_error(message: str, exit_code: int = 1) -> None:
    STATE["error"] = message
    STATE["exit_code"] = exit_code
    STATE["phase"] = "error"


def run_extract_job(
    source: str,
    cookie: str | None,
    demo: bool,
    output_dir: str,
    *,
    note_mode: str = "plain",
    vault_path: str = "",
    vault_subdir: str = "",
) -> None:
    try:
        if not demo and cookie:
            cookie, cookie_note = normalize_cookie(cookie)
            if cookie_note:
                _log(cookie_note)
            if "sessdata" not in cookie.lower():
                _finish_error(cookie_note or "Cookie 中没有 SESSDATA 字段。", 2)
                return
        if demo:
            # 演示模式同样支持粘贴视频链接（Mock 的 view 路由会反查出演示合集）；
            # 输入为空时使用默认演示合集链接
            from demo.run_demo import DEMO_SOURCE

            start_demo()
            source = (source or "").strip() or DEMO_SOURCE
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        with BilibiliClient(cookie=cookie or None) as client:
            outcome = run_collection(
                source, Path(output_dir), client, log=_log, note_mode=note_mode
            )
        STATE["summary"] = summarize(outcome)
        STATE["exit_code"] = 1 if has_failure(outcome) else 0
        STATE["collection_name"] = outcome.collection_name
        STATE["files"] = _badge_files_extract(outcome, Path(output_dir))
        if note_mode == "obsidian" and vault_path:
            cfg = load_config()
            if vault_subdir:
                cfg.subdir = vault_subdir
            STATE["obsidian_open"] = _obsidian_uri(
                vault_path, _vault_rel_index_path(cfg, outcome.collection_name)
            )
        STATE["phase"] = "done"
    except ValueError as exc:
        _finish_error(str(exc), 2)
    except Exception as exc:  # noqa: BLE001 - 网页界面兜底展示
        _finish_error(f"{type(exc).__name__}: {exc}")
    finally:
        if demo:
            stop_demo()
        STATE["running"] = False


def run_migrate_job(
    source_dir: str, collections: list[str] | None, overwrite: bool
) -> None:
    try:
        outcome = migrate(
            Path(source_dir),
            load_config(),
            collections,
            overwrite=overwrite,
            log=_log,
        )
        STATE["summary"] = format_migration_summary(outcome)
        failed = any(
            f.status == "failed" for r in outcome.results for f in r.files
        )
        STATE["exit_code"] = 1 if failed else 0
        STATE["files"] = []
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
        _finish_error(str(exc), 2)
    except Exception as exc:  # noqa: BLE001 - 网页界面兜底展示
        _finish_error(f"{type(exc).__name__}: {exc}")
    finally:
        STATE["running"] = False


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", INDEX_HTML.read_bytes())
        elif path == "/api/run":
            with _lock:
                snapshot = {
                    key: (list(value) if isinstance(value, list) else value)
                    for key, value in STATE.items()
                }
            self._json(snapshot)
        elif path == "/api/config":
            self._json(load_config().model_dump())
        elif path == "/api/browse":
            # 目录浏览：path 为空返回盘符列表（Windows），否则返回该目录下的子文件夹。
            # 本地个人工具，配合网页「浏览…」按钮代替手动输入路径
            q = parse_qs(urlparse(self.path).query)
            raw = (q.get("path") or [""])[0].strip()
            if not raw:
                import string

                drives = [
                    {"name": f"{d}:", "path": f"{d}:\\"}
                    for d in string.ascii_uppercase
                    if Path(f"{d}:/").exists()
                ]
                self._json({"current": "", "parent": None, "dirs": drives})
                return
            target = Path(raw).expanduser()
            if not target.is_dir():
                self._json({"error": f"目录不存在：{raw}"}, 400)
                return
            dirs = sorted(
                (
                    child.name
                    for child in target.iterdir()
                    if child.is_dir() and not child.name.startswith("$")
                ),
                key=str.casefold,
            )
            parent = target.parent
            self._json(
                {
                    "current": str(target),
                    "parent": str(parent) if parent != target else None,
                    "dirs": dirs,
                }
            )
        elif path == "/api/file":
            q = parse_qs(urlparse(self.path).query)
            name = (q.get("name") or [""])[0]
            if not STATE.get("collection_name"):
                self._json({"error": "还没有可查看的文件"}, 400)
                return
            base = (Path(STATE["output_dir"]) / STATE["collection_name"]).resolve()
            target = (base / name).resolve()
            if (
                target.suffix != ".md"
                or base not in target.parents  # 防目录穿越
                or not target.is_file()
            ):
                self._json({"error": "文件不存在"}, 404)
                return
            self._json({"name": target.name, "content": target.read_text(encoding="utf-8")})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return

        if path == "/api/config":
            cfg = load_config()
            if isinstance(data.get("vault"), str):
                cfg.vault = data["vault"].strip()
            if isinstance(data.get("subdir"), str) and data["subdir"].strip():
                cfg.subdir = data["subdir"].strip()
            save_config(cfg)
            self._json(cfg.model_dump())
            return

        if path == "/api/check-vault":
            result = check_vault(
                (data.get("path") or "").strip(), create=bool(data.get("create"))
            )
            self._json(result.model_dump())
            return

        if path == "/api/migrate-scan":
            dir_path = (data.get("dir") or "").strip()
            try:
                scan = scan_collections(Path(dir_path))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            self._json(scan.model_dump(mode="json"))
            return

        if path == "/api/check-cookie":
            # 登录态检测：粘贴后先验证，避免空跑全部集数
            raw = (data.get("cookie") or "").strip()
            if not raw:
                self._json({"ok": False, "message": "请先粘贴 Cookie"}, 400)
                return
            cookie, note = normalize_cookie(raw)
            if "sessdata" not in cookie.lower():
                self._json({"ok": False, "message": note or "Cookie 中没有 SESSDATA 字段。"})
                return
            try:
                with BilibiliClient(cookie=cookie) as client:
                    logged_in, uname = client.whoami()
            except Exception as exc:  # noqa: BLE001 - 检测失败给出原因
                self._json({"ok": False, "message": f"检测失败：{exc}"})
                return
            if logged_in:
                suffix = f"（{note}）" if note else ""
                self._json({"ok": True, "message": f"登录态有效：{uname}{suffix}"})
            else:
                self._json(
                    {
                        "ok": False,
                        "message": "Cookie 无效或已过期：B站认为当前未登录。"
                        "请重新复制完整 Cookie（需在已登录的浏览器中获取）。",
                    }
                )
            return

        if path == "/api/preview":
            # 提取前审查：只提取第 1 集，返回成品 Markdown 与审查报告
            with _lock:
                if STATE["running"]:
                    self._json({"error": "已有任务在运行中，请稍候"}, 409)
                    return
            demo = bool(data.get("demo"))
            source = (data.get("source") or "").strip()
            cookie = (data.get("cookie") or "").strip() or os.environ.get("BILI_COOKIE") or ""
            vault = (data.get("vault") or "").strip()
            if not demo and not source:
                self._json({"error": "缺少合集链接或 season_id"}, 400)
                return
            try:
                if not demo and cookie:
                    cookie, _ = normalize_cookie(cookie)
                    if "sessdata" not in cookie.lower():
                        self._json({"error": "Cookie 中没有 SESSDATA 字段，无法获取字幕列表"}, 400)
                        return
                lines: list[str] = []
                if demo:
                    from demo.run_demo import DEMO_SOURCE

                    start_demo()
                    source = source or DEMO_SOURCE
                try:
                    with BilibiliClient(cookie=cookie or None) as client:
                        result = preview_first_episode(
                            source, client, log=lambda line: lines.append(line),
                            note_mode="obsidian" if vault else "plain",
                        )
                finally:
                    if demo:
                        stop_demo()
                payload = result.model_dump(mode="json")
                payload["preview_log"] = lines
                payload["format_report"] = format_preview(result)
                self._json(payload)
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if path not in ("/api/extract", "/api/migrate"):
            self._json({"error": "not found"}, 404)
            return

        with _lock:
            if STATE["running"]:
                self._json({"error": "已有任务在运行中，请稍候"}, 409)
                return
            STATE.clear()
            STATE.update(_fresh_state())

        if path == "/api/extract":
            demo = bool(data.get("demo"))
            source = (data.get("source") or "").strip()
            cookie = (data.get("cookie") or "").strip() or os.environ.get("BILI_COOKIE") or ""
            vault = (data.get("vault") or "").strip()
            vault_subdir = (data.get("vault_subdir") or "").strip()
            try:
                if vault:
                    # 传入即记住（PRD §5.1）；同时决定输出落点与笔记格式
                    cfg = load_config()
                    cfg.vault = vault
                    if vault_subdir:
                        cfg.subdir = vault_subdir
                    save_config(cfg)
                    cfg = load_config()
                    output = str(collection_root(cfg))
                    note_mode = "obsidian"
                else:
                    output = (data.get("output") or "").strip() or str(REPO_ROOT / "output")
                    note_mode = "plain"
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            if not demo and not source:
                self._json({"error": "缺少合集链接或 season_id"}, 400)
                return
            with _lock:
                STATE.update(
                    running=True,
                    phase="running",
                    kind="extract",
                    demo=demo,
                    source=(source or "").strip() or ("(内置演示合集)" if demo else ""),
                    output_dir=output,
                    note_mode=note_mode,
                    vault=vault,
                )
            threading.Thread(
                target=run_extract_job,
                args=(source, cookie, demo, output),
                kwargs={
                    "note_mode": note_mode,
                    "vault_path": vault,
                    "vault_subdir": vault_subdir,
                },
                daemon=True,
            ).start()
            self._json({"started": True})
            return

        # /api/migrate
        source_dir = (data.get("dir") or "").strip()
        collections = data.get("collections") or None
        overwrite = bool(data.get("overwrite"))
        vault = (data.get("vault") or "").strip()
        vault_subdir = (data.get("vault_subdir") or "").strip()
        if not source_dir:
            self._json({"error": "缺少旧字幕目录"}, 400)
            return
        try:
            if not Path(source_dir).is_dir():
                self._json({"error": f"旧字幕目录不存在：{source_dir}"}, 400)
                return
            if vault:
                cfg = load_config()
                cfg.vault = vault
                if vault_subdir:
                    cfg.subdir = vault_subdir
                save_config(cfg)
        except OSError as exc:
            self._json({"error": f"目录不可访问：{exc}"}, 400)
            return
        with _lock:
            STATE.update(
                running=True,
                phase="running",
                kind="migrate",
                source=source_dir,
                vault=vault,
            )
        names = [s.strip() for s in collections if str(s).strip()] if collections else None
        threading.Thread(
            target=run_migrate_job,
            args=(source_dir, names, overwrite),
            daemon=True,
        ).start()
        self._json({"started": True})

    def log_message(self, format: str, *args: object) -> None:  # 静默访问日志
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="B站合集字幕提取器 · 网页界面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    try:
        server = LocalServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"端口 {args.port} 无法监听：{exc}", file=sys.stderr)
        print(
            "很可能是旧的 web/server.py 还在运行：请先关闭它的窗口（或用任务管理器结束"
            " python 进程），再重新启动；也可以换一个端口，如 --port 8766。",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    print(f"PORT={port}", flush=True)
    print(f"B站合集字幕提取器网页界面已启动：{url}", flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
