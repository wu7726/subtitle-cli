"""接口响应的 pydantic 模型与领域数据模型。

只声明本项目用到的字段，接口响应中的其余字段一律忽略，
字段变化时只需改这里（技术方案 §4）。
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

try:
    from enum import StrEnum  # Python 3.11+
except ImportError:  # Python 3.10：str() / f-string 表现为成员值，对齐 StrEnum 语义
    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return str(self.value)


# ---- 领域模型（跨层流转的数据） ----
class Episode(BaseModel):
    """合集内的一集。cid 取字幕前可能未知（需 pagelist 补齐）。

    source_url / transcript_url 是跨平台通用字段（播客等复用 Episode），
    B站路径不使用、保持默认值。
    """

    bvid: str
    cid: str | None = None
    title: str
    index: int  # 合集内序号，从 1 开始
    source_url: str = ""  # 单集页面链接（属性头 source 优先取它）
    transcript_url: str | None = None  # 现成文稿地址（播客；B站不使用）
    audio_url: str = ""  # 音频/视频直链（ASR 兜底下载用；B站经 playurl 动态获取）
    published: str = ""  # 发布日期（抖音 create_time 等；B站不使用）


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


class DashAudioStream(BaseModel):
    """playurl DASH 的一条音频流（只需取流地址）。"""

    id: int = 0
    bandwidth: int = 0
    base_url: str = Field(default="", alias="baseUrl")
    backup_url: list[str] = Field(default_factory=list, alias="backupUrl")

    model_config = {"populate_by_name": True}

    def best_url(self) -> str:
        """主地址优先，备选地址兜底（CDN 单点偶发失败）。"""
        return self.base_url or (self.backup_url[0] if self.backup_url else "")


class DashData(BaseModel):
    """playurl 的 dash 部分（只要音频流列表）。"""

    audio: list[DashAudioStream] = Field(default_factory=list)


class PlayurlData(BaseModel):
    """x/player/wbi/playurl 的 data 部分（ASR 兜底下载音频用）。"""

    dash: DashData = Field(default_factory=DashData)


class UgcSeason(BaseModel):
    """view 接口返回的视频所属合集信息。"""

    id: int
    title: str = ""


class VideoOwner(BaseModel):
    """视频 UP 主（view 接口的 owner 字段，笔记属性 author 来源）。"""

    name: str = ""


class ViewData(BaseModel):
    """x/web-interface/wbi/view 的 data 部分（只声明用到的字段）。"""

    bvid: str = ""
    aid: int | None = None
    title: str = ""
    videos: int = 1  # 分P数量
    ugc_season: UgcSeason | None = None
    owner: VideoOwner | None = None
