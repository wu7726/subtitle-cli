"""接口响应的 pydantic 模型与领域数据模型。

只声明本项目用到的字段，接口响应中的其余字段一律忽略，
字段变化时只需改这里（技术方案 §4）。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


# ---- 领域模型（跨层流转的数据） ----
class Episode(BaseModel):
    """合集内的一集。cid 取字幕前可能未知（需 pagelist 补齐）。"""

    bvid: str
    cid: str | None = None
    title: str
    index: int  # 合集内序号，从 1 开始


class SubtitleLine(BaseModel):
    from_time: float = Field(alias="from")
    to_time: float = Field(alias="to")
    content: str

    model_config = {"populate_by_name": True}


class SubtitleTrack(BaseModel):
    """一条字幕轨：语言 + 逐行内容。"""

    lan: str  # 如 zh-CN（人工CC）/ ai-zh（AI字幕）
    lines: list[SubtitleLine]


class EpisodeStatus(StrEnum):
    SUCCESS = "success"  # 新下载成功
    SKIPPED = "skipped"  # 文件已存在，增量跳过（汇总计入成功组并单独标注）
    NO_SUBTITLE = "no_subtitle"
    FAILED = "failed"  # 网络错误 / 风控 / 解析失败，带 reason


class EpisodeResult(BaseModel):
    episode: Episode
    status: EpisodeStatus
    reason: str | None = None


# ---- 接口响应模型（仅声明用到的字段） ----
class ApiResponse(BaseModel):
    """B站 API 统一外层结构。"""

    code: int
    message: str = ""


class SeasonMeta(BaseModel):
    name: str = ""


class ArchiveItem(BaseModel):
    bvid: str
    title: str
    part: str = ""  # 分P标题，合集场景常与 title 重复或更短


class SeasonArchivesPage(BaseModel):
    """seasons_archives_list 的 data 部分。"""

    meta: SeasonMeta = Field(default_factory=SeasonMeta)
    archives: list[ArchiveItem] = Field(default_factory=list)
    page: dict = Field(default_factory=dict)


class SubtitleItem(BaseModel):
    """player/wbi/v2 返回的一条可用字幕。"""

    lan: str
    lan_doc: str = ""
    subtitle_url: str = Field(default="")


class PlayerSubtitleInfo(BaseModel):
    subtitles: list[SubtitleItem] = Field(default_factory=list)


class PlayerData(BaseModel):
    subtitle: PlayerSubtitleInfo = Field(default_factory=PlayerSubtitleInfo)


class PageInfo(BaseModel):
    cid: int
    page: int = 1
    part: str = ""
