"""离线 Demo 黑盒测试：demo/run_demo.py 以子进程完整跑一遍，验证产物与输出。

该测试同时充当「无网络端到端」回归：CLI、pipeline、converter、storage
全部走真实代码，仅网络层为本地 Mock。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_SCRIPT = REPO_ROOT / "demo" / "run_demo.py"
COLLECTION = "示例合集·美食漫谈"


def test_demo_end_to_end_offline(tmp_path: Path):
    out = tmp_path / "out"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(DEMO_SCRIPT), "--output", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"

    ep_dir = out / COLLECTION
    # EP05 刻意无字幕 → 5 个 Markdown 文件
    files = sorted(ep_dir.glob("*.md"))
    assert [f.name for f in files] == [
        "EP01 早餐的哲学.md",
        "EP02 一碗面的讲究.md",
        "EP03 深夜食堂的救赎.md",
        "EP04 甜咸之争的终局.md",
        "EP06 外卖时代的自处.md",
    ]

    # CC 轨：有标点的段落正文，H1 = 第N集 标题
    ep1 = (ep_dir / "EP01 早餐的哲学.md").read_text(encoding="utf-8")
    assert ep1.startswith("# 第1集 早餐的哲学\n\n")
    assert "大家好，欢迎来到美食漫谈的第一集。" in ep1
    assert ep1.endswith("\n")
    assert b"\r" not in ep1.encode("utf-8")  # LF 落盘

    # EP03 只有 AI 轨 → 回退 ai-zh（典型特征：无标点）
    ep3 = (ep_dir / "EP03 深夜食堂的救赎.md").read_text(encoding="utf-8")
    assert ep3.startswith("# 第3集 深夜食堂的救赎\n\n")
    assert "夜宵的意义不只是填饱肚子" in ep3
    assert "。" not in ep3

    # 汇总输出：第一次真实下载、第二次全部增量跳过、EP05 无字幕
    assert "成功 5（其中增量跳过 0）" in proc.stdout
    assert "成功 5（其中增量跳过 5）" in proc.stdout
    assert "无字幕  1：EP05" in proc.stdout
    assert "已存在，跳过" in proc.stdout
    assert proc.stdout.count("退出码 0") == 1  # 第一次运行退出码 0
