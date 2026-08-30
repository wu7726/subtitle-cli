"""wbi 签名单测：用社区固定输入输出向量锁定行为（技术方案 §5.2、§9）。

向量来源：bilibili-API-collect wbi 文档。2026-08-30 已核对：
- nav 实测返回的 img_key/sub_key 与文档示例一致；
- 本实现计算的 mixin_key、w_rid 与文档断言值逐字符一致。
"""

from subtitle_cli.bilibili.wbi import get_mixin_key, sign

IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"


def test_mixin_key_matches_community_vector():
    assert get_mixin_key(IMG_KEY, SUB_KEY) == "ea1db124af3c7062474693fa704f4ff8"


def test_sign_matches_community_vector():
    signed = sign(
        {"foo": "114", "bar": "514", "zab": "1919810"}, IMG_KEY, SUB_KEY, 1702204169
    )
    assert signed["w_rid"] == "8f6f2b5b3d485fe1886cec6a0be8c5d4"
    assert signed["wts"] == "1702204169"
    # 参数按名排序，w_rid 在签名计算后追加
    assert list(signed) == ["bar", "foo", "wts", "zab", "w_rid"]


def test_sign_filters_special_chars():
    signed = sign({"foo": "a!b'c(d)e*f"}, IMG_KEY, SUB_KEY, 1)
    assert signed["foo"] == "abcdef"  # !'()* 从参数值中过滤
    assert len(signed["w_rid"]) == 32
    assert all(ch in "0123456789abcdef" for ch in signed["w_rid"])


def test_sign_is_deterministic():
    a = sign({"a": 1, "b": "x"}, IMG_KEY, SUB_KEY, 123)
    b = sign({"a": 1, "b": "x"}, IMG_KEY, SUB_KEY, 123)
    assert a == b


def test_sign_does_not_mutate_input():
    params = {"a": 1}
    sign(params, IMG_KEY, SUB_KEY, 123)
    assert params == {"a": 1}
