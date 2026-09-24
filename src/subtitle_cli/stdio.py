"""console script 共用的终端管道小事，只依赖标准库。

独立成模块而不是塞进 cli.py：subtitle-cli-migrate 必须保持「不加载B站模块」
（test_migration.py 有断言），所以它不能从 cli.py 里借东西。
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """Windows 下重定向输出时默认用本地编码，统一改为 UTF-8 防乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
