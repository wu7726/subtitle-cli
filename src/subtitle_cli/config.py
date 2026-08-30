"""全局常量：限速间隔、超时、请求头等配置。

集中放置可调参数，实测校准后只改这里（技术方案 §5.4、M4）。
"""

from __future__ import annotations

# ---- HTTP 基础 ----
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://www.bilibili.com/"
REQUEST_TIMEOUT = 15.0  # 秒，单请求超时

# ---- 限速（串行 + 随机间隔，秒）----
LIST_DELAY_RANGE = (0.5, 1.5)  # 分页/列表类请求间隔
MEDIA_DELAY_RANGE = (1.5, 3.0)  # player/字幕类请求间隔

# ---- 重试与风控 ----
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0  # 指数退避基数：2s/4s/8s
# 仅对这些信号重试：HTTP 412/429 风控、B站业务码 -352/-412
RISK_HTTP_STATUS = {412, 429}
RISK_BIZ_CODES = {-352, -412}
# 连续 N 集风控失败则提前终止，提示等待后重跑
RISK_ABORT_THRESHOLD = 5

# ---- 转换层 ----
PARAGRAPH_MIN_CHARS = 80  # 段长达到该值且行尾为句末标点时闭合段落
PARAGRAPH_PAUSE_SECONDS = 2.0  # 与上一行起点间隔超过该值视为说话停顿，换段

# ---- 存储 ----
TITLE_MAX_CHARS = 60  # 文件名中标题截断长度，保证整路径远低于 260

# ---- 本地语音转写（ASR 兜底，多平台扩展计划 §3 第 2 步）----
ASR_MODEL_SIZE = "small"  # 默认模型：small（约 480MB，中文质量与 CPU 速度的平衡点）
ASR_DEVICE = "auto"  # auto：有 NVIDIA 显卡用 CUDA，否则 CPU
ASR_MODEL_DIR = "~/.subtitle-cli/models"  # 模型下载目录（可删除重下）
ASR_LOG_EVERY_SEGMENTS = 20  # 转写进度日志间隔（段数）
