"""本地 HTTP 服务基类：ThreadingHTTPServer + Windows 端口绑定修复。

主服务（web/server.py）与离线演示 Mock（jobs.start_demo）共用。
"""

from __future__ import annotations

import os
from http.server import ThreadingHTTPServer


class LocalServer(ThreadingHTTPServer):
    """本地服务。Windows 下禁用 SO_REUSEADDR：默认行为允许第二个进程静默
    绑定同一端口，请求会被随机路由到（可能僵死的）旧实例，表现为页面能
    打开但接口时好时坏（Failed to fetch）。改为显式失败并提示。"""

    allow_reuse_address = os.name != "nt"
    daemon_threads = True
