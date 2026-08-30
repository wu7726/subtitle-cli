# -*- coding: utf-8 -*-
"""离线 Demo：一键跑通 subtitle-cli 全流程（无需 Cookie、无需真实网络）。

原理：在本机起一个临时 Mock 服务，回放B站接口的响应结构（与 tests/fixtures
同构，含 wbi 签名请求），并把接口层 URL 指向它。CLI、pipeline、converter、
storage 全部走真实代码路径，仅网络层被替换。

演示合集共 6 集，刻意覆盖三类结果：
    EP01/02/04  人工 CC（zh-CN）+ AI 字幕 → 选 CC 轨（有标点）
    EP03        只有 AI 字幕（ai-zh）     → 回退 AI 轨（无标点）
    EP05        无任何字幕                → 无字幕分支
    EP06        人工 CC                   → 正常下载

用法：
    python demo/run_demo.py                  # 输出到仓库根目录 demo_output/
    python demo/run_demo.py --output D:/tmp  # 自定义输出目录
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))  # 未 pip install 时也可直接运行

from subtitle_cli import config  # noqa: E402
from subtitle_cli.bilibili import client as client_module  # noqa: E402
from subtitle_cli.cli import app  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

COLLECTION_NAME = "示例合集·美食漫谈"
SEASON_ID = "888888"
DEMO_SOURCE = f"https://space.bilibili.com/1000/channel/collectiondetail?sid={SEASON_ID}"

# (序号, 标题, 段落文本, 字幕形态)：form = "cc" | "ai" | None
EPISODES: list[tuple[int, str, list[str], str | None]] = [
    (
        1,
        "早餐的哲学",
        [
            "大家好，欢迎来到美食漫谈的第一集。今天我们从一个最平凡的话题讲起，那就是早餐。",
            "很多人把早餐当成任务，随便对付两口就出门。但其实一天的心情，往往是从这一顿开始的。",
            "一碗热粥，一颗溏心蛋，再加一碟小咸菜。简单的搭配里，藏着中国人对一天的郑重其事。",
        ],
        "cc",
    ),
    (
        2,
        "一碗面的讲究",
        [
            "北方人做面，讲究的是筋道，揉面醒面一步都不能省。",
            "南方人做面，讲究的是汤头，一锅老汤要吊足几个小时。",
            "一碗面里，其实装着两种截然不同的生活节奏。",
        ],
        "cc",
    ),
    (
        3,
        "深夜食堂的救赎",
        [
            "夜宵的意义不只是填饱肚子。更是给加班的人留一盏灯。",
            "街角那家只开到凌晨两点的小店。老板记得每个熟客的口味。",
            "一碗馄饨下肚。这一天就算有了交代。",
        ],
        "ai",
    ),
    (
        4,
        "甜咸之争的终局",
        [
            "豆花到底是甜的还是咸的，这个问题吵了几十年。",
            "其实答案很简单，你从小吃到大的那一种，就是最好吃的那一种。",
            "口味没有对错，只有乡愁。",
        ],
        "cc",
    ),
    (5, "厨房里的物理学", [], None),  # 无任何字幕 → 无字幕分支
    (
        6,
        "外卖时代的自处",
        [
            "外卖改变的不只是吃饭方式，还有人和厨房的关系。",
            "这期节目我们聊聊，如何在便利和自己动手之间找到平衡。",
            "每周给自己认真做一顿饭，是对生活最基本的诚意。",
        ],
        "cc",
    ),
]


# ---- 字幕数据生成 ----
def _wrap(text: str, width: int) -> list[str]:
    return [text[i : i + width] for i in range(0, len(text), width)]


def _sentences(text: str) -> list[str]:
    return re.findall(r"[^。！？]+[。！？]|[^。！？]+", text)


def build_body(paragraphs: list[str]) -> list[dict]:
    """把段落文本展开成带时间轴的字幕行。

    段内行间隔 < 2 秒（不触发换段），段间停顿 2.8 秒（触发 converter 换段），
    这样 Markdown 输出的分段与这里书写的段落一一对应。
    """
    body: list[dict] = []
    t = 0.6
    for para in paragraphs:
        for sentence in _sentences(para):
            for piece in _wrap(sentence, 14):
                dur = min(1.8, max(1.0, len(piece) * 0.1))
                body.append(
                    {"from": round(t, 2), "to": round(t + dur, 2), "location": 0, "content": piece}
                )
                t += dur
        t += 2.8  # 段间停顿
    return body


def subtitle_payload(ep_index: int, form: str) -> dict:
    paragraphs = next(ep[2] for ep in EPISODES if ep[0] == ep_index)
    if form == "ai":  # AI 字幕的典型特征：没有标点
        paragraphs = [re.sub(r"[，。！？、…；：,.\s]", "", p) for p in paragraphs]
    return {"body": build_body(paragraphs)}


def bvid_of(index: int) -> str:
    return f"BV1DE0000{index:03d}"  # BV + 10 位，与真实 BV 号长度一致


DEMO_VIDEO_URL = f"https://www.bilibili.com/video/{bvid_of(1)}/"  # 演示合集第 1 集


def index_of(bvid: str) -> int:
    return int(bvid[-3:])


# ---- Mock 服务 ----
def make_handler() -> type[BaseHTTPRequestHandler]:
    """构造 Mock 处理器。

    注意：真实B站返回的字幕 URL 是绝对地址（hdslb.com 域），Mock 同样返回
    基于自身端口的绝对 URL，保证客户端行为与真实场景一致。
    """

    class MockHandler(BaseHTTPRequestHandler):
        def _send_json(self, payload: dict, status: int = 200) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            url = urlparse(self.path)
            q = parse_qs(url.query)
            path = url.path
            if path == "/x/web-interface/nav":
                self._send_json(
                    {
                        "code": -101,
                        "message": "账号未登录",
                        "data": {
                            "wbi_img": {
                                "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                                "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
                            }
                        },
                    }
                )
            elif path == "/x/polymer/web-space/seasons_archives_list":
                self._send_json(
                    {
                        "code": 0,
                        "message": "0",
                        "data": {
                            "meta": {"name": COLLECTION_NAME, "total": len(EPISODES)},
                            "archives": [
                                {"bvid": bvid_of(index), "title": title}
                                for index, title, _, _ in EPISODES
                            ],
                            "page": {
                                "page_num": int(q.get("page_num", ["1"])[0]),
                                "page_size": int(q.get("page_size", ["100"])[0]),
                                "total": len(EPISODES),
                            },
                        },
                    }
                )
            elif path == "/x/web-interface/wbi/view":
                # 视频 → 所属合集（演示模式下任何 BV 都指向演示合集）
                self._send_json(
                    {
                        "code": 0,
                        "message": "0",
                        "data": {
                            "bvid": q.get("bvid", [""])[0],
                            "ugc_season": {"id": int(SEASON_ID), "title": COLLECTION_NAME},
                            "owner": {"name": "演示UP主"},
                        },
                    }
                )
            elif path == "/x/player/pagelist":
                index = index_of(q["bvid"][0])
                cid = 900000 + index
                self._send_json(
                    {"code": 0, "message": "0", "data": [{"cid": cid, "page": 1, "part": ""}]}
                )
            elif path == "/x/player/wbi/v2":
                index = index_of(q["bvid"][0])
                form = next(ep[3] for ep in EPISODES if ep[0] == index)
                subtitles = []
                if form == "cc":
                    subtitles = [
                        {"lan": "ai-zh", "lan_doc": "AI字幕", "subtitle_url": f"{base}/subtitle/{index}_ai.json"},
                        {"lan": "zh-CN", "lan_doc": "中文字幕", "subtitle_url": f"{base}/subtitle/{index}.json"},
                    ]
                elif form == "ai":
                    subtitles = [
                        {"lan": "ai-zh", "lan_doc": "AI字幕", "subtitle_url": f"{base}/subtitle/{index}_ai.json"}
                    ]
                self._send_json(
                    {"code": 0, "message": "0", "data": {"subtitle": {"subtitles": subtitles}}}
                )
            elif path.startswith("/subtitle/"):
                name = path[len("/subtitle/") : -len(".json")]
                if name.endswith("_ai"):
                    index, form = int(name[:-3]), "ai"
                else:
                    index, form = int(name), "cc"
                ep_form = next(ep[3] for ep in EPISODES if ep[0] == index)
                if ep_form != form:
                    self._send_json({"code": -404, "message": "no such subtitle"}, status=404)
                else:
                    self._send_json(subtitle_payload(index, form))
            else:
                self._send_json({"code": -404, "message": f"demo mock 未知路径: {path}"}, status=404)

        def log_message(self, format: str, *args: object) -> None:  # 静默请求日志
            pass

    return MockHandler


# ---- 运行 ----
def patch_client_to(base: str, *, speedup: bool = True):
    """把接口层指向指定 base（Mock 服务），返回恢复函数。

    speedup=True 时压缩随机间隔（演示模式）；真实运行保持 config 默认值。
    注意：client 新增的端点 URL 常量必须同步加入此处，否则会打到真实接口。
    """
    originals = [
        (client_module, "API_BASE", client_module.API_BASE),
        (client_module, "SEASON_ARCHIVES_URL", client_module.SEASON_ARCHIVES_URL),
        (client_module, "PAGELIST_URL", client_module.PAGELIST_URL),
        (client_module, "PLAYER_V2_URL", client_module.PLAYER_V2_URL),
        (client_module, "NAV_URL", client_module.NAV_URL),
        (client_module, "VIEW_URL", client_module.VIEW_URL),
        (config, "LIST_DELAY_RANGE", config.LIST_DELAY_RANGE),
        (config, "MEDIA_DELAY_RANGE", config.MEDIA_DELAY_RANGE),
    ]
    client_module.API_BASE = base
    client_module.SEASON_ARCHIVES_URL = f"{base}/x/polymer/web-space/seasons_archives_list"
    client_module.PAGELIST_URL = f"{base}/x/player/pagelist"
    client_module.PLAYER_V2_URL = f"{base}/x/player/wbi/v2"
    client_module.NAV_URL = f"{base}/x/web-interface/nav"
    client_module.VIEW_URL = f"{base}/x/web-interface/wbi/view"
    if speedup:
        config.LIST_DELAY_RANGE = (0.02, 0.06)
        config.MEDIA_DELAY_RANGE = (0.03, 0.09)

    def restore() -> None:
        for obj, attr, value in originals:
            setattr(obj, attr, value)

    return restore


def main() -> None:
    parser = argparse.ArgumentParser(description="subtitle-cli 离线演示")
    parser.add_argument("--output", default=str(REPO_ROOT / "demo_output"), help="输出目录")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    restore = patch_client_to(base)

    source = DEMO_SOURCE
    runner = CliRunner()
    try:
        print(f"Mock 接口已启动：{base}")
        print(f"演示合集：《{COLLECTION_NAME}》共 {len(EPISODES)} 集（EP05 刻意无字幕）")
        print(f"输出目录：{Path(args.output).resolve()}")
        print()
        print("=" * 46, "第一次运行（真实下载）", "=" * 46)
        result = runner.invoke(app, [source, "--output", args.output])
        print(result.output)
        print(f"（退出码 {result.exit_code}）")
        print()
        print("=" * 46, "第二次运行（验证增量跳过）", "=" * 46)
        result2 = runner.invoke(app, [source, "--output", args.output])
        print(result2.output)
    finally:
        restore()
        server.shutdown()
        server.server_close()

    out_dir = Path(args.output) / COLLECTION_NAME
    files = sorted(out_dir.glob("*.md"))
    print()
    print("=" * 46, "生成成果", "=" * 46)
    for f in files:
        print(f"  {f.name}  ({f.stat().st_size} 字节)")
    if files:
        print()
        print(f"---- {files[0].name} 内容预览 ----")
        for line in files[0].read_text(encoding="utf-8").splitlines()[:10]:
            print(f"  {line}")
    print()
    print("真实运行方式（需要B站 Cookie，详见 README）：")
    print('  subtitle-cli <合集URL或season_id> --cookie "SESSDATA=..."')


if __name__ == "__main__":
    main()
