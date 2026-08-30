"""播客接口层：RSS 解析、文稿下载与解析、HTTP 客户端。

与 bilibili/ 同级的新平台网络边界；纯函数解析部分无 I/O，可离线单测。
"""

from .client import PodcastClient, PodcastError

__all__ = ["PodcastClient", "PodcastError"]
