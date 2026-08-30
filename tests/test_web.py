"""网页界面黑盒测试：以子进程启动 web/server.py，走完 API 全流程。

覆盖：页面可达、离线演示全流程（含二次增量）、成果文件读取、
真实模式非法输入的退出码 2。全程仅访问本机回环地址。
"""

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


def test_web_ui_offline_demo_and_error_paths(tmp_path: Path):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
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
    try:
        first_line = proc.stdout.readline().strip()
        assert first_line.startswith("PORT="), first_line
        base = f"http://127.0.0.1:{first_line.split('=', 1)[1]}"

        # 页面可达
        status, html = _get(base, "/")
        assert status == 200 and "B站合集字幕提取器" in html

        # 离线演示：一次真实下载
        status, resp = _post(base, "/api/extract", {"demo": True, "output": str(tmp_path / "out")})
        assert status == 200 and resp.get("started")
        state = _wait_done(base)
        assert state["phase"] == "done" and state["exit_code"] == 0
        assert "成功 5（其中增量跳过 0）" in state["summary"]
        assert "无字幕  1：EP05" in state["summary"]
        assert len(state["files"]) == 5
        assert any("EP01 早餐的哲学.md" in name for name in state["files"])

        # 读取成果文件（Markdown 内容）
        query = urllib.parse.urlencode({"name": state["files"][0]})
        status, doc = _get(base, "/api/file?" + query)
        assert status == 200
        doc = json.loads(doc)
        assert doc["content"].startswith("# 第1集 早餐的哲学\n")
        assert "。" in doc["content"]  # CC 轨有标点

        # 目录穿越防护（服务端应拒绝）
        try:
            status, _ = _get(base, "/api/file?" + urllib.parse.urlencode({"name": "../x.md"}))
            assert status in (400, 404)
        except urllib.error.HTTPError as exc:
            assert exc.code in (400, 404)

        # 二次运行：全部增量跳过
        _post(base, "/api/extract", {"demo": True, "output": str(tmp_path / "out")})
        state = _wait_done(base)
        assert "成功 5（其中增量跳过 5）" in state["summary"]

        # 真实模式 + 非法输入（纯解析即可判定，不触网）→ 错误信息与退出码 2
        status, resp = _post(
            base, "/api/extract",
            {"demo": False, "source": "https://www.bilibili.com/list/546195", "output": str(tmp_path / "out2")},
        )
        assert status == 200
        state = _wait_done(base)
        assert state["phase"] == "error" and state["exit_code"] == 2
        assert "无法从输入中识别合集" in (state["error"] or "")

        # 演示模式 + 视频链接输入：BV → view 反查合集 → 爬取整个合集（全新输出目录）
        status, resp = _post(
            base, "/api/extract",
            {
                "demo": True,
                "source": "https://www.bilibili.com/video/BV1DE0000001/",
                "output": str(tmp_path / "out_video"),
            },
        )
        assert status == 200
        state = _wait_done(base)
        assert state["phase"] == "done" and state["exit_code"] == 0
        assert "成功 5（其中增量跳过 0）" in state["summary"]
        assert len(state["files"]) == 5

        # 运行中重复提交 → 409
        _post(base, "/api/extract", {"demo": True, "output": str(tmp_path / "out3")})
        status, resp = _post(base, "/api/extract", {"demo": True, "output": str(tmp_path / "out4")})
        assert status == 409
        _wait_done(base)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
