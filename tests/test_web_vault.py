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
        assert note.startswith("---\n") and 'source: ""' in note
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
        # vault 模式下每条产物带 obsidian:// 单集直达链接
        assert all(
            f.get("obsidian_uri", "").startswith("obsidian://open?vault=")
            for f in state["files"]
        )
        # 文件日志记录任务起止（事后取证渠道）；Cookie 纪律同样适用于日志
        log_text = (tmp_path / "_logs" / "subtitle-cli.log").read_text(encoding="utf-8")
        assert "提取开始" in log_text and "提取结束" in log_text
        assert "SESSDATA" not in log_text
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
    """页面包含 vault/目录选择器/Obsidian 直达关键元素；迁移卡已移除。"""
    proc, base = _start_server(tmp_path)
    try:
        status, html = _get(base, "/")
        assert status == 200
        for key in [
            'id="vaultInput"', 'id="vaultSubdirInput"',
            'id="openObsidian"', 'id="outVault"', 'id="outFolder"',
            '写入 Obsidian vault（推荐）', '创建该文件夹',
            'id="outVault" checked', '在 Obsidian 中打开合集索引',
            'id="fileProtoBanner"',  # file:// 直接打开时的引导横幅
            'id="browseModal"', '浏览…', '选择此文件夹',
        ]:
            assert key in html, key
        # 迁移卡已按用户要求从网页移除（能力保留在 CLI：subtitle-cli-migrate）
        assert 'id="migrateCard"' not in html
        assert "迁移旧字幕" not in html
        # 默认态：输出位置区块随演示模式隐藏
        assert 'id="outputSection" hidden' in html
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


def test_browse_api(tmp_path: Path):
    """/api/browse：盘符列表、子目录枚举、非法路径 400（问题2 目录浏览）。"""
    proc, base = _start_server(tmp_path)
    try:
        status, text = _get(base, "/api/browse?path=")
        assert status == 200
        drives = json.loads(text)
        if os.name == "nt":
            # Windows：空路径 = 「此电脑」盘符列表
            assert drives["current"] == "" and drives["parent"] is None
            assert drives["dirs"] and all("name" in d and "path" in d for d in drives["dirs"])
        else:
            # 其他系统没有盘符概念：空路径直接列根目录
            assert drives["current"] and drives["parent"] == ""
            assert drives["dirs"] and all("name" in d and "path" in d for d in drives["dirs"])

        status, text = _get(
            base, "/api/browse?path=" + urllib.parse.quote(str(REPO_ROOT))
        )
        assert status == 200
        repo = json.loads(text)
        assert {"docs", "src", "web", "tests"} <= {d["name"] for d in repo["dirs"]}
        assert repo["parent"]

        if os.name == "nt":
            # 盘符根目录：上级为空串（回到「此电脑」），而非 None
            status, text = _get(base, "/api/browse?path=" + urllib.parse.quote("C:/"))
            root = json.loads(text)
            assert status == 200 and root["parent"] == ""

        try:
            _get(base, "/api/browse?path=" + urllib.parse.quote(str(tmp_path / "nope")))
            raise AssertionError("应当返回 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_web_rejects_dead_vault_root(tmp_path: Path):
    """失效 vault 根：提取/迁移一律 400 拒绝，绝不静默 mkdir 重建死路径，
    也不把死路径写进配置（对齐 CLI「校验通过才写回」的既有纪律）。"""
    proc, base = _start_server(tmp_path)
    try:
        dead = tmp_path / "ghost-vault"
        status, resp = _post(base, "/api/extract", {"demo": True, "vault": str(dead)})
        assert status == 400 and "vault 路径不可用" in resp["error"]
        assert not dead.exists()  # 未被盲目重建
        status, text = _get(base, "/api/config")
        assert json.loads(text)["vault"] == ""  # 死路径未被记住

        legacy = make_legacy(tmp_path)
        status, resp = _post(
            base, "/api/migrate", {"dir": str(legacy), "vault": str(dead)}
        )
        assert status == 400 and "vault 路径不可用" in resp["error"]
        assert not dead.exists()
        status, text = _get(base, "/api/config")
        assert json.loads(text)["vault"] == ""
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_web_history_api_and_card(tmp_path: Path):
    """历史卡：/api/history 读 runs 状态表（与 subtitle-cli-status 同源），
    页面渲染最近合集；无历史时不显示卡片。"""
    from subtitle_cli import state as state_mod
    from subtitle_cli.bilibili.models import Episode, EpisodeStatus

    # 无历史：端点返回空
    proc, base = _start_server(tmp_path)
    try:
        status, resp = _get(base, "/api/history")
        assert status == 200 and json.loads(resp)["entries"] == []
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    # 造两条历史（一条含失败/无字幕），重启服务
    # 状态目录由 conftest 的 SUBTITLE_CLI_STATE_DIR 指向 tmp_path/_state，子进程继承
    st = state_mod.CollectionState(season_id="123", output_dir=str(tmp_path / "out"),
                                   collection_name="示例合集")
    state_mod.record_episode(st, Episode(bvid="BV1", title="EP01", index=1), EpisodeStatus.SUCCESS)
    state_mod.record_episode(st, Episode(bvid="BV2", title="EP02", index=2), EpisodeStatus.SKIPPED)
    state_mod.record_episode(st, Episode(bvid="BV3", title="EP03", index=3), EpisodeStatus.NO_SUBTITLE)
    state_mod.save_collection(st)
    st2 = state_mod.CollectionState(season_id="456", output_dir=str(tmp_path / "out2"),
                                    collection_name="播客·某某节目")
    state_mod.record_episode(st2, Episode(bvid="BV9", title="第1期", index=1), EpisodeStatus.FAILED)
    state_mod.save_collection(st2)

    proc, base = _start_server(tmp_path)
    try:
        status, text = _get(base, "/api/history")
        entries = json.loads(text)["entries"]
        assert status == 200 and len(entries) == 2
        # 按最近更新倒序，第一条是刚保存的示例合集
        assert entries[0]["name"] == "示例合集"
        assert entries[0]["season_id"] == "123"
        assert entries[0]["success"] == 2 and entries[0]["skipped"] == 1
        assert entries[0]["nosub"] == 1 and entries[0]["fail"] == 0
        assert entries[1]["fail"] == 1

        status, html = _get(base, "/")
        assert 'id="historyCard"' in html and 'id="historyList"' in html
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_web_history_delete(tmp_path: Path):
    """删除一条 runs 记录：/api/history/delete 按 (season_id, output_dir) 定位，
    删除后列表不再包含；用户笔记目录不受影响。"""
    from subtitle_cli import state as state_mod
    from subtitle_cli.bilibili.models import Episode, EpisodeStatus

    st = state_mod.CollectionState(season_id="123", output_dir=str(tmp_path / "out"),
                                   collection_name="示例合集")
    state_mod.record_episode(st, Episode(bvid="BV1", title="EP01", index=1), EpisodeStatus.SUCCESS)
    path = state_mod.save_collection(st)
    notes_dir = tmp_path / "out" / "示例合集"
    notes_dir.mkdir(parents=True)
    (notes_dir / "EP01 标题1.md").write_text("# 笔记", encoding="utf-8")

    proc, base = _start_server(tmp_path)
    try:
        status, resp = _post(
            base,
            "/api/history/delete",
            {"season_id": "123", "output_dir": str(tmp_path / "out")},
        )
        assert status == 200 and resp["deleted"] is True
        assert not path.exists()
        status, text = _get(base, "/api/history")
        assert json.loads(text)["entries"] == []
        assert (notes_dir / "EP01 标题1.md").exists()  # 笔记分毫未动
        # 缺 season_id → 400
        status, resp = _post(base, "/api/history/delete", {})
        assert status == 400
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_web_merge_api(tmp_path: Path):
    """/api/merge：合集目录合并导出，目录穿越被拒，缺参 400。"""
    proc, base = _start_server(tmp_path)
    try:
        out = tmp_path / "out" / "示例合集"
        out.mkdir(parents=True)
        (out / "EP01 一.md").write_text("---\ntitle: x\n---\n\n第一集。", encoding="utf-8")
        (out / "EP02 二.md").write_text("第二集。", encoding="utf-8")

        status, resp = _post(
            base,
            "/api/merge",
            {"dir": str(tmp_path / "out"), "collection": "示例合集"},
        )
        assert status == 200 and resp["name"] == "示例合集-全文.md"
        merged = out / "示例合集-全文.md"
        text = merged.read_text(encoding="utf-8")
        assert "## EP01 一" in text and "第二集。" in text
        assert "title:" not in text

        # 目录穿越：collection 经清洗不会逃出合集目录
        status, resp = _post(
            base,
            "/api/merge",
            {"dir": str(tmp_path / "out"), "collection": "../../secret"},
        )
        assert status in (200, 400)
        if status == 200:
            assert not (tmp_path / "secret-全文.md").exists()

        # 缺参 → 400
        status, resp = _post(base, "/api/merge", {"dir": str(tmp_path / "out")})
        assert status == 400
        # 空目录 → 400 无可合并分集
        (tmp_path / "empty" / "空合集").mkdir(parents=True)
        status, resp = _post(
            base, "/api/merge", {"dir": str(tmp_path / "empty"), "collection": "空合集"}
        )
        assert status == 400 and "没有可合并" in resp["error"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
