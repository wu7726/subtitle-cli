"""任务共享状态：单任务模型的内存态、互斥锁与文件日志。

轮询端点 /api/run 快照的就是这里的 STATE；任务线程（jobs）只经
本模块的函数改写，前端不直接写。
"""

from __future__ import annotations

import threading

from subtitle_cli.logging_setup import setup_logging

_lock = threading.Lock()
file_log = setup_logging()  # 轮转文件日志：任务起止与异常 traceback 的事后取证


def fresh_state() -> dict:
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


STATE: dict = fresh_state()


def log_line(line: str) -> None:
    STATE["log"].append({"line": line})


def finish_error(message: str, exit_code: int = 1) -> None:
    STATE["error"] = message
    STATE["exit_code"] = exit_code
    STATE["phase"] = "error"
