"""网页 vault 能力黑盒测试（M9）：config/check-vault/migrate-scan/migrate 与
demo+vault 提取链路。全程离线（Mock + 本地迁移），配置文件经环境变量隔离。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "web" / "server.py"


def _get(base: str, path: str) -> tuple[int, str]:
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_done(base: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = json.loads(_get(base, "/api/run")[1])
        if not state["running"]:
            return state
        time.sleep(0.2)
    raise AssertionError("任务未在时限内结束")


def _start_server(tmp_path: Path):
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "SUBTITLE_CLI_CONFIG": str(tmp_path / "web-config.json"),
    }
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", "0", "--no-open"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(REPO_ROOT),
    )
    first_line = proc.stdout.readline().strip()
    assert first_line.startswith("PORT="), first_line
    base = f"http://127.0.0.1:{first_line.split('=', 1)[1]}"
    return proc, base


def make_legacy(tmp_path: Path) -> Path:
    root = tmp_path / "old"
    cdir = root / "旧合集"
    cdir.mkdir(parents=True)
    (cdir / "EP01 第一集.md").write_text("# 第1集 第一集\n\n大家好。\n", encoding="utf-8")
    (cdir / "EP02 第二集.md").write_text("# 第2集 第二集\n\n内容A。\n", encoding="utf-8")
    return root


def test_web_vault_apis(tmp_path: Path):
    proc, base = _start_server(tmp_path)
    try:
        vault = tmp_path / "vault"
        (vault / ".obsidian").mkdir(parents=True)  # 有效 vault 根

        # ---- /api/config 读写 ----
        status, cfg = _get(base, "/api/config")
        cfg = json.loads(cfg)
        assert status == 200 and cfg["vault"] == "" and cfg["subdir"] == "B站字幕"
        status, cfg = _post(base, "/api/config", {"vault": str(vault)})
        assert status == 200 and cfg["vault"] == str(vault)
        status, text = _get(base, "/api/config")
        assert status == 200 and json.loads(text)["vault"] == str(vault)  # 已持久化

        # ---- /api/check-vault 三态 ----
        status, resp = _post(base, "/api/check-vault", {"path": str(vault)})
        assert status == 200 and resp["ok"] and resp["is_vault_root"]
        (vault / "笔记").mkdir()  # ⚠️ 分支要求「存在且可写」的子目录
        status, resp = _post(base, "/api/check-vault", {"path": str(vault / "笔记")})
        assert resp["ok"] and not resp["is_vault_root"]  # ⚠️ 子目录
        status, resp = _post(base, "/api/check-vault", {"path": str(tmp_path / "nope")})
        assert not resp["ok"] and resp["can_create"]  # ❌ 不存在
        status, resp = _post(
            base, "/api/check-vault", {"path": str(tmp_path / "made"), "create": True}
        )
        assert resp["ok"] and (tmp_path / "made").is_dir()  # 创建后可写
        status, resp = _post(base, "/api/check-vault", {"path": "  "})
        assert not resp["ok"] and "为空" in resp["message"]

        # ---- /api/migrate-scan ----
        legacy = make_legacy(tmp_path)
        status, scan = _post(base, "/api/migrate-scan", {"dir": str(legacy)})
        assert status == 200
        assert [c["name"] for c in scan["collections"]] == ["旧合集"]
        assert len(scan["collections"][0]["pending"]) == 2
        status, resp = _post(base, "/api/migrate-scan", {"dir": str(tmp_path / "absent")})
        assert status == 400 and "不存在" in resp["error"]

        # ---- /api/migrate 全流程（离线） ----
        status, resp = _post(
            base,
            "/api/migrate",
            {"dir": str(legacy), "vault": str(vault)},
        )
        assert status == 200 and resp.get("started")
        state = _wait_done(base)
        assert state["phase"] == "done" and state["exit_code"] == 0, state
        assert "成功 2（迁移 2、跳过 0）" in state["summary"]
        assert state["kind"] == "migrate"
        target = vault / "B站字幕" / "旧合集"
        note = (target / "EP01 第一集.md").read_text(encoding="utf-8")
        assert note.startswith("---\n") and 'bvid: ""' in note
        assert state["files"][0]["badge"] == "索引"
        assert any(f["badge"] == "迁移" for f in state["files"])
        # vault 根有效 → 提供 obsidian:// 打开链接
        assert state["obsidian_open"] and state["obsidian_open"].startswith("obsidian://open?vault=")
        assert urllib.parse.quote("旧合集") in state["obsidian_open"]

        # 二次迁移：全部跳过
        status, _ = _post(base, "/api/migrate", {"dir": str(legacy), "vault": str(vault)})
        state = _wait_done(base)
        assert state["exit_code"] == 0 and "迁移 0" in state["summary"]
        assert any(f["badge"] == "跳过" for f in state["files"])

        # ---- demo + vault 提取链路（离线验证 vault 直写与徽标/URI） ----
        status, resp = _post(
            base,
            "/api/extract",
            {"demo": True, "vault": str(vault), "vault_subdir": "学习/字幕"},
        )
        assert status == 200 and resp.get("started")
        state = _wait_done(base)
        assert state["phase"] == "done" and state["exit_code"] == 0, state
        assert state["note_mode"] == "obsidian"
        assert state["files"][0]["badge"] == "索引"
        # 索引页 + 分集在嵌套子目录下
        nested = vault / "学习" / "字幕" / "示例合集·美食漫谈"
        assert (nested / "示例合集·美食漫谈.md").exists()
        ep1 = (nested / "EP01 早餐的哲学.md").read_text(encoding="utf-8")
        assert ep1.startswith("---\n")
        # /api/file 能读 vault 内文件（白名单随任务落点）
        status, doc = _get(
            base, "/api/file?" + urllib.parse.urlencode({"name": state["files"][0]["name"]})
        )
        doc = json.loads(doc)
        assert status == 200 and doc["content"].startswith("---\n")

        # 迁移 + 提取互斥：提取运行中提交迁移 → 409（演示任务耗时足够长）
        _post(base, "/api/extract", {"demo": True, "output": str(tmp_path / "x")})
        status, _ = _post(
            base,
            "/api/migrate",
            {"dir": str(legacy), "vault": str(vault), "overwrite": True},
        )
        assert status == 409
        _wait_done(base)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_page_structure_vault_elements(tmp_path: Path):
    """M10：页面包含 vault/迁移/Obsidian 直达的关键元素与默认态。"""
    proc, base = _start_server(tmp_path)
    try:
        status, html = _get(base, "/")
        assert status == 200
        for key in [
            'id="vaultInput"', 'id="vaultSubdirInput"', 'id="migrateCard"',
            'id="scanBtn"', 'id="migrateBtn"', 'id="overwriteChk"',
            'id="openObsidian"', 'id="outVault"', 'id="outFolder"',
            '写入 Obsidian vault（推荐）', '检查 vault', '创建该文件夹',
            'id="outVault" checked', '在 Obsidian 中打开合集索引',
        ]:
            assert key in html, key
        # 默认态：输出位置区块随演示模式隐藏；迁移卡常显
        assert 'id="outputSection" hidden' in html
        assert '<section class="card" id="migrateCard">' in html
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_port_conflict_fails_loudly(tmp_path: Path):
    """Windows 下端口被占时必须显式报错退出（回归：曾因 SO_REUSEADDR 静默
    双绑定，请求被随机路由到僵死实例 → 页面正常但接口 Failed to fetch）。"""
    if os.name != "nt":
        import pytest

        pytest.skip("仅 Windows 存在静默双绑定问题")
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        proc = subprocess.run(
            [sys.executable, str(SERVER), "--port", str(port), "--no-open"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 1
        assert "端口" in (proc.stderr + proc.stdout)
        assert "无法监听" in (proc.stderr + proc.stdout)
    finally:
        blocker.close()
