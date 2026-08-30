"""跨平台的错误基类。

B站与播客等各平台客户端的异常都继承 PlatformError，
pipeline / CLI 按基类捕获，新增平台不用改上层 except 分支。
"""

from __future__ import annotations


class PlatformError(Exception):
    """平台客户端的接口调用失败（网络、解析、业务错误）。"""
