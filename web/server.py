"""本地网页界面入口。

    python web/server.py            # 默认 http://127.0.0.1:8765，自动打开浏览器
    python web/server.py --port 0   # 随机端口（打印 PORT=xxx 供测试读取）

两种模式：
- 离线演示：复用 demo/ 的本地 Mock（无需 Cookie、不访问真实网络）
- 真实接口：直连 api.bilibili.com，Cookie 只在本次运行中透传给 API 域，
  不写日志、不落盘（与 CLI 一致）

路由与任务实现见 web/app/ 包（handler.py 的模块文档列出了全部端点）。
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from web.app.handler import Handler  # noqa: E402
from web.app.httpd import LocalServer  # noqa: E402


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
