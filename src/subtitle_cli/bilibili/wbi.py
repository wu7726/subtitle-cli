"""wbi 签名（技术方案 §5.2）。

混淆表来自社区维护的 bilibili-API-collect，若签名请求开始返回 -403，
优先核对这张表是否更新（产品文档 §5 风险对策）。
"""

from __future__ import annotations

import hashlib
import urllib.parse

# FIXED 混淆表（社区维护，写死）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# 参数值中需要过滤的字符
_FILTER_CHARS = set("!'()*")


def get_mixin_key(img_key: str, sub_key: str) -> str:
    """img_key + sub_key 按混淆表重排，取前 32 位。"""
    orig = img_key + sub_key  # 共 64 字符
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign(
    params: dict[str, object], img_key: str, sub_key: str, wts: int
) -> dict[str, str]:
    """对请求参数做 wbi 签名，返回含 wts 与 w_rid 的新字典（已排序）。

    纯函数：不取时间戳、不发请求，wts 由调用方传入以便固定输入做单测。
    """
    mixin = get_mixin_key(img_key, sub_key)
    signed = {k: str(v) for k, v in params.items()}
    signed["wts"] = str(wts)
    signed = dict(sorted(signed.items()))
    signed = {
        k: "".join(ch for ch in v if ch not in _FILTER_CHARS)
        for k, v in signed.items()
    }
    query = urllib.parse.urlencode(signed)
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return signed
