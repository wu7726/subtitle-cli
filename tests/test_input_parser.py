"""输入解析单测：resolve_input 各 URL 形态与报错（技术方案 §6 输入解析规则）。

BV 号 → season_id 需要调用 view 接口（有网络副作用），其解析与网络路径
分别在 extract_bvid 纯函数（本文件）与 test_client.py（MockTransport）中测试。
"""

import pytest

from subtitle_cli.bilibili.client import BilibiliClient, extract_bvid


@pytest.fixture
def client() -> BilibiliClient:
    c = BilibiliClient()
    yield c
    c.close()


# ---- BV 号提取（纯函数，无网络） ----
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BV1xx411c7mD", "BV1xx411c7mD"),
        ("  BV1xx411c7mD  ", "BV1xx411c7mD"),
        (
            "https://www.bilibili.com/video/BV17TtA6VEuH/?p=1&share_medium=android",
            "BV17TtA6VEuH",
        ),
        ("https://b23.tv/xxxxx 视频 BV1QUVb6uEyn", "BV1QUVb6uEyn"),
        ("好看的视频：BV1DE1234567。", "BV1DE1234567"),
        ("没有视频号", None),
        ("https://space.bilibili.com/546195/channel/collectiondetail?sid=8016518", None),
    ],
)
def test_extract_bvid(raw: str, expected: str | None):
    assert extract_bvid(raw) == expected


# ---- resolve_input：纯解析路径（无网络） ----
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12345", "12345"),
        ("  8016518  ", "8016518"),
        (
            "https://space.bilibili.com/546195/channel/collectiondetail?sid=8016518",
            "8016518",
        ),
        (
            "https://space.bilibili.com/546195/channel/collectiondetail?season_id=123",
            "123",
        ),
        ("https://www.bilibili.com/list/546195?sid=456&seid=789", "456"),
        # 无 scheme 的链接也应能解析
        ("space.bilibili.com/546195/channel/collectiondetail?sid=999", "999"),
        # 多个参数时取 sid/season_id 的值
        ("https://www.bilibili.com/x?a=1&sid=42", "42"),
    ],
)
def test_resolve_valid(client: BilibiliClient, raw: str, expected: str):
    assert client.resolve_input(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc",
        "https://www.bilibili.com/list/546195",  # URL 但无 sid 也无 BV 号
    ],
)
def test_resolve_invalid_raises(client: BilibiliClient, raw: str):
    with pytest.raises(ValueError):
        client.resolve_input(raw)


def test_error_message_mentions_supported_forms(client: BilibiliClient):
    with pytest.raises(ValueError) as excinfo:
        client.resolve_input("不认识的输入")
    message = str(excinfo.value)
    assert "sid" in message and "BV" in message
