# 测试 Fixture 说明

除特别标注外，均为 **2026-08-30 M0 接口实测** 录制的真实响应（未登录态）。

| 文件 | 来源接口 | 用途 | 录制方式 |
| --- | --- | --- | --- |
| `seasons_archives_list_p1.json` | `seasons_archives_list?season_id=8016518&page_num=1&page_size=100` | 分集列表解析（58 集单页） | 实测录制 |
| `seasons_archives_list_p1_size20.json` | 同上，`page_size=20` | 分页遍历测试第 1 页 | 实测录制 |
| `seasons_archives_list_p2_size20.json` | 同上，`page_num=2` | 分页遍历测试第 2 页 | 实测录制 |
| `pagelist.json` | `x/player/pagelist?bvid=BV17TtA6VEuH` | 取 cid | 实测录制 |
| `player_v2_nocookie.json` | `x/player/wbi/v2`（已签名、未登录） | 无字幕分支（未登录时 subtitles 恒为空） | 实测录制 |
| `subtitle.json` | 字幕 JSON 下载 | 字幕行解析、converter 段落规则 | **构造**（见下） |

## 关于 `subtitle.json`

`player/wbi/v2` 的字幕列表**必须携带登录态（SESSDATA）**才非空，无登录环境无法在线录制字幕 JSON。
本文件按社区文档（bilibili-API-collect）记录的结构 `{"body": [{"from", "to", "location", "content"}]}` 构造，
内容为虚构字幕文本。集成测试（`pytest -m integration`，携带真实 Cookie）会对该结构做在线校验。

## 实测结论摘要（详见 docs/技术方案.md §5）

- `seasons_archives_list` 无需 wbi 签名、无需 Cookie；
- `pagelist` 无需签名，`view` 接口在本环境被 412 风控（因此选 pagelist）；
- `player/wbi/v2` 签名与不签名均可返回 code=0，但无 Cookie 时字幕列表为空；
- wbi 签名与社区固定向量一致（mixin_key、w_rid 均匹配）。
