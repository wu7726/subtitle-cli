# -*- coding: utf-8 -*-
"""本地网页界面：在浏览器里运行字幕提取并查看生成的 Markdown。

    python web/server.py            # 默认 http://127.0.0.1:8765，自动打开浏览器
    python web/server.py --port 0   # 随机端口（打印 PORT=xxx 供测试读取）

两种模式：
- 离线演示：复用 demo/ 的本地 Mock（无需 Cookie、不访问真实网络）
- 真实接口：直连 api.bilibili.com，Cookie 只在本次运行中透传给 API 域，
  不写日志、不落盘（与 CLI 一致）

API：
    GET  /                 页面
    GET  /api/run          当前运行状态（轮询）
    GET  /api/file?name=   读取某集 Markdown（限制在本次输出目录内）
    POST /api/extract      {demo, source, cookie, output} → 后台线程执行
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
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from subtitle_cli import config  # noqa: E402
from subtitle_cli.bilibili.client import BilibiliClient, normalize_cookie  # noqa: E402
from subtitle_cli.pipeline import (  # noqa: E402
    format_preview,
    has_failure,
    preview_first_episode,
    run_collection,
    summarize,
)

INDEX_HTML = REPO_ROOT / "web" / "index.html"

_lock = threading.Lock()


def _fresh_state() -> dict:
    return {
        "running": False,
        "phase": "idle",  # idle | running | done | error
        "demo": False,
        "source": "",
        "output_dir": "",
        "log": [],  # 逐行进度（run_collection 的 log 回调输出）
        "summary": None,
        "exit_code": None,
        "collection_name": None,
        "files": [],
        "error": None,
    }


STATE: dict = _fresh_state()


def start_demo() -> str:
    """启动本地 Mock 并把接口层指过去，返回其 base URL（恢复用 stop_demo）。"""
    from demo.run_demo import make_handler, patch_client_to

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
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


def run_job(source: str, cookie: str | None, demo: bool, output_dir: str) -> None:
    restore = None
    try:
        if not demo and cookie:
            cookie, cookie_note = normalize_cookie(cookie)
            if cookie_note:
                STATE["log"].append({"line": cookie_note})
            if "sessdata" not in cookie.lower():
                STATE["error"] = cookie_note or "Cookie 中没有 SESSDATA 字段。"
                STATE["exit_code"] = 2
                STATE["phase"] = "error"
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
                source, Path(output_dir), client,
                log=lambda line: STATE["log"].append({"line": line}),
            )
        STATE["summary"] = summarize(outcome)
        STATE["exit_code"] = 1 if has_failure(outcome) else 0
        STATE["collection_name"] = outcome.collection_name
        ep_dir = Path(output_dir) / outcome.collection_name
        STATE["files"] = sorted(p.name for p in ep_dir.glob("*.md")) if ep_dir.is_dir() else []
        STATE["phase"] = "done"
    except ValueError as exc:
        STATE["error"] = str(exc)
        STATE["exit_code"] = 2
        STATE["phase"] = "error"
    except Exception as exc:  # noqa: BLE001 - 网页界面兜底展示
        STATE["error"] = f"{type(exc).__name__}: {exc}"
        STATE["phase"] = "error"
    finally:
        if demo:
            stop_demo()
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
                            source, client, log=lambda line: lines.append(line)
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

        if path != "/api/extract":
            self._json({"error": "not found"}, 404)
            return
        demo = bool(data.get("demo"))
        source = (data.get("source") or "").strip()
        cookie = (data.get("cookie") or "").strip() or os.environ.get("BILI_COOKIE") or ""
        output = (data.get("output") or "").strip() or str(REPO_ROOT / "output")
        if not demo and not source:
            self._json({"error": "缺少合集链接或 season_id"}, 400)
            return
        with _lock:
            if STATE["running"]:
                self._json({"error": "已有任务在运行中，请稍候"}, 409)
                return
            STATE.clear()
            STATE.update(_fresh_state())
            STATE.update(
                running=True,
                phase="running",
                demo=demo,
                source=(source or "").strip() or ("(内置演示合集)" if demo else ""),
                output_dir=output,
            )
        threading.Thread(target=run_job, args=(source, cookie, demo, output), daemon=True).start()
        self._json({"started": True})

    def log_message(self, format: str, *args: object) -> None:  # 静默访问日志
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="B站合集字幕提取器 · 网页界面")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
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
