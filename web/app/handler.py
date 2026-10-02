"""HTTP 路由：回环门禁、静态页与全部 API 端点。

    GET  /                  页面
    GET  /api/run           当前运行状态（轮询）
    GET  /api/health        轻量探活（启动器区分「已在运行」与「端口被占」）
    GET  /api/history       最近运行历史（runs 状态表只读镜像，最近 10 条）
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

import json
import os
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from subtitle_cli import state as runs_state
from subtitle_cli.bilibili.client import BilibiliClient, resolve_bilibili_cookie
from subtitle_cli.bilibili.models import EpisodeStatus
from subtitle_cli.config import ASR_MODEL_CHOICES, ASR_MODEL_SIZE
from subtitle_cli.dispatch import (
    BILIBILI,
    PODCAST,
    create_client,
    detect_platform,
    platform_subdir,
    resolve_proxy,
    set_platform_subdir,
)
from subtitle_cli.migration import scan_collections
from subtitle_cli.pipeline import (
    format_preview,
    preview_first_episode,
)
from subtitle_cli.vault import check_vault, collection_root, load_config, save_config

from .jobs import run_extract_job, run_migrate_job, start_demo, stop_demo
from .state import STATE, _lock, file_log, fresh_state, log_line

REPO_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO_ROOT / "web" / "index.html"


class Handler(BaseHTTPRequestHandler):
    _LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

    def _access_allowed(self, *, post: bool) -> bool:
        """回环门禁：Host 必须是本机回环 + 本服务端口（防 DNS rebinding，
        rebinding 请求的 Host 是攻击者域名）；POST 额外校验 Origin/Referer
        同源（防恶意网页跨站表单提交）。无 Origin/Referer 的请求视为
        curl/测试等非浏览器工具，放行。"""
        port = self.server.server_address[1]
        allowed_hosts = {f"{h}:{port}" for h in self._LOOPBACK_HOSTS}
        if port == 80:
            allowed_hosts |= set(self._LOOPBACK_HOSTS)
        if self.headers.get("Host", "") not in allowed_hosts:
            self._json({"error": "拒绝访问：Host 不是本机回环地址"}, 403)
            return False
        if post and not self._browser_origin_ok(port):
            self._json({"error": "拒绝访问：跨站请求"}, 403)
            return False
        return True

    def _browser_origin_ok(self, port: int) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            referer = self.headers.get("Referer")
            if referer is None:
                return True
            origin = referer
        parts = urlparse(origin)
        return (
            parts.scheme == "http"
            and (parts.hostname or "") in self._LOOPBACK_HOSTS
            and (parts.port or 80) == port
        )

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def do_GET(self) -> None:
        if not self._access_allowed(post=False):
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", INDEX_HTML.read_bytes())
        elif path == "/api/health":
            # 轻量探活：启动器用它区分「本工具已在运行」与「端口被别的程序占用」
            self._json({"app": "subtitle-cli", "ok": True})
        elif path == "/api/history":
            # 只读历史：最近运行的合集（复用 runs 状态表，与 subtitle-cli-status 同源）
            entries = []
            try:
                states = runs_state.iter_collections()[:10]
            except OSError:
                states = []
            for s in states:
                counts = {"success": 0, "skipped": 0, "nosub": 0, "fail": 0}
                for ep in s.episodes.values():
                    if ep.status in (EpisodeStatus.SUCCESS, EpisodeStatus.SKIPPED):
                        counts["success"] += 1
                    elif ep.status == EpisodeStatus.NO_SUBTITLE:
                        counts["nosub"] += 1
                    elif ep.status == EpisodeStatus.FAILED:
                        counts["fail"] += 1
                counts["skipped"] = sum(
                    1 for ep in s.episodes.values() if ep.status == EpisodeStatus.SKIPPED
                )
                entries.append(
                    {
                        "name": s.collection_name or s.season_id,
                        "season_id": s.season_id,
                        "updated": s.updated,
                        "output_dir": s.output_dir,
                        "total": len(s.episodes),
                        **counts,
                    }
                )
            self._json({"entries": entries})
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
            # 目录浏览：path 为空时 Windows 返回盘符列表（「此电脑」），
            # 其他系统没有盘符概念，直接列根目录
            q = parse_qs(urlparse(self.path).query)
            raw = (q.get("path") or [""])[0].strip()
            if not raw and os.name == "nt":
                import string

                drives = [
                    {"name": f"{d}:", "path": f"{d}:\\"}
                    for d in string.ascii_uppercase
                    if Path(f"{d}:/").exists()
                ]
                self._json({"current": "", "parent": None, "dirs": drives})
                return
            if not raw:
                raw = "/"
            target = Path(raw).expanduser()
            if not target.is_dir():
                self._json({"error": f"目录不存在：{raw}"}, 400)
                return
            dirs = sorted(
                (
                    {"name": child.name, "path": str(child)}
                    for child in target.iterdir()
                    if child.is_dir() and not child.name.startswith("$")
                ),
                key=lambda item: item["name"].casefold(),
            )
            parent = target.parent
            self._json(
                {
                    "current": str(target),
                    # 盘符根目录的上级 = 空串（回到「此电脑」盘符列表）
                    "parent": "" if parent == target else str(parent),
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

    def do_POST(self) -> None:
        if not self._access_allowed(post=True):
            return
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return

        if path == "/api/history/delete":
            # 删除一条 runs 状态记录（工具自有的结论缓存；不碰任何用户笔记）。
            # (season_id, output_dir) 是状态文件的复合身份，缺一不可
            season_id = (data.get("season_id") or "").strip()
            output_dir = (data.get("output_dir") or "").strip()
            if not season_id:
                self._json({"error": "缺少 season_id"}, 400)
                return
            target = runs_state.collection_state_path(season_id, output_dir)
            try:
                target.unlink(missing_ok=True)
            except OSError as exc:
                self._json({"error": f"删除失败：{exc}"}, 500)
                return
            self._json({"deleted": True})
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
            # 登录态检测：粘贴后先验证，避免空跑全部集数。
            # 只看显式粘贴的内容，不回退 BILI_COOKIE（按钮语义就是"验证粘贴的这段"）
            raw = (data.get("cookie") or "").strip()
            if not raw:
                self._json({"ok": False, "message": "请先粘贴 Cookie"}, 400)
                return
            try:
                cookie, note = resolve_bilibili_cookie(raw)
            except ValueError as exc:
                self._json({"ok": False, "message": str(exc)})
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
            cookie = (data.get("cookie") or "").strip()
            vault = (data.get("vault") or "").strip()
            if not demo and not source:
                self._json({"error": "缺少合集链接或 season_id"}, 400)
                return
            try:
                platform = detect_platform(source)
                if demo and platform == PODCAST:
                    self._json({"error": "演示模式使用内置B站示例数据，仅支持B站输入"}, 400)
                    return
                if not demo and platform == BILIBILI:
                    cookie, _ = resolve_bilibili_cookie(cookie)
                proxy = resolve_proxy(data.get("proxy"))
                lines: list[str] = []
                if demo:
                    from demo.run_demo import DEMO_SOURCE

                    start_demo()
                    source = source or DEMO_SOURCE
                try:
                    with create_client(source, cookie or None, proxy=proxy) as client:
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
            except Exception as exc:  # noqa: BLE001 - traceback 进文件日志
                file_log.exception("预览异常")
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
            STATE.update(fresh_state())

        if path == "/api/extract":
            demo = bool(data.get("demo"))
            source = (data.get("source") or "").strip()
            vault = (data.get("vault") or "").strip()
            vault_subdir = (data.get("vault_subdir") or "").strip()
            subdir_used = vault_subdir
            try:
                platform = detect_platform(source)
                if demo and platform != BILIBILI:
                    self._json({"error": "演示模式使用内置B站示例数据，仅支持B站输入"}, 400)
                    return
                if not demo and not source:
                    self._json({"error": "缺少合集链接或 season_id"}, 400)
                    return
                cookie = ""
                if not demo and platform == BILIBILI:
                    try:
                        cookie, note = resolve_bilibili_cookie(data.get("cookie"))
                    except ValueError as exc:
                        self._json({"error": str(exc)}, 400)
                        return
                    if note:
                        log_line(note)
                if vault:
                    # 传入即记住（PRD §5.1）。校验通过才写回、才建目录（与 CLI
                    # 同一纪律）：失效 vault 根一律拒绝，绝不静默 mkdir 重建死路径
                    status = check_vault(vault)
                    if not status.ok:
                        self._json(
                            {
                                "error": f"vault 路径不可用：{vault}（{status.message}）；"
                                "可先在「检查 vault」中创建该文件夹"
                            },
                            400,
                        )
                        return
                    cfg = load_config()
                    cfg.vault = vault
                    if vault_subdir:
                        set_platform_subdir(cfg, platform, vault_subdir)
                    save_config(cfg)
                    subdir_used = vault_subdir or platform_subdir(cfg, platform)
                    output = str(collection_root(cfg, subdir_used))
                    note_mode = "obsidian"
                else:
                    output = (data.get("output") or "").strip() or str(REPO_ROOT / "output")
                    note_mode = "plain"
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            asr = bool(data.get("asr")) and not demo
            asr_model = str(data.get("asr_model") or ASR_MODEL_SIZE)
            if asr_model not in ASR_MODEL_CHOICES:
                asr_model = ASR_MODEL_SIZE
            proxy = resolve_proxy(data.get("proxy"))
            asr_prompt = (data.get("asr_prompt") or "").strip() or None
            asr_limit_raw = data.get("asr_limit")
            asr_limit = (
                int(asr_limit_raw)
                if isinstance(asr_limit_raw, (int, float)) and asr_limit_raw > 0
                else None
            )
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

            def _progress(done: int, total: int) -> None:
                """任务线程回调：写入真实进度供 /api/run 轮询。"""
                with _lock:
                    STATE["progress"] = {"done": done, "total": total}

            threading.Thread(
                target=run_extract_job,
                args=(source, cookie, demo, output),
                kwargs={
                    "note_mode": note_mode,
                    "vault_path": vault,
                    "vault_subdir": subdir_used,
                    "asr": asr,
                    "asr_model": asr_model,
                    "asr_limit": asr_limit,
                    "on_progress": _progress,
                    "proxy": proxy,
                    "asr_prompt": asr_prompt,
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
                # 与提取同一纪律：失效 vault 根拒绝并写回前拦截
                status = check_vault(vault)
                if not status.ok:
                    self._json({"error": f"vault 路径不可用：{vault}（{status.message}）"}, 400)
                    return
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
