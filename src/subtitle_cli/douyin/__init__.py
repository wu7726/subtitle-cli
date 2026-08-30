"""抖音平台接口层：单视频信息解析与媒体下载（多平台扩展计划 §4）。

探测结论（2026-08-30 实测）：
- 分享页 SSR / 旧 iteminfo 接口 / 蜘蛛 UA 均已无视频数据；
- www.douyin.com 的 detail 接口当前只需 ttwid Cookie（bytedance 接口可注册）
  + 伪 msToken，无需 a_bogus 签名即可返回完整视频信息（可能随平台调整失效，
  失效时报错会引导更新）；
- douyinvod.com CDN 直链可裸下载（mp4 音视频混流，faster-whisper 经 PyAV 解码音轨）。
"""

from .client import DouyinClient, DouyinError

__all__ = ["DouyinClient", "DouyinError"]
