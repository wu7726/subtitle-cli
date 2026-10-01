"""pipeline 的 ASR 兜底单测：无字幕 → 下载音频 → 转写 → 出笔记，全程离线打桩。"""

from __future__ import annotations

from pathlib import Path

import pytest

from subtitle_cli import asr as asr_mod
from subtitle_cli import state
from subtitle_cli.asr import AsrDependencyError
from subtitle_cli.bilibili.models import Episode, EpisodeStatus, SubtitleLine, SubtitleTrack
from subtitle_cli.errors import PlatformError
from subtitle_cli.pipeline import run_collection, summarize

from tests.test_pipeline import FakeClient  # 复用无 download_audio 的基础假客户端


class FakeAsrClient(FakeClient):
    """在 FakeClient 上补 download_audio：把"音频内容"写到目标文件。"""

    download_error: Exception | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.downloads: list[str] = []  # 每次真实下载记一个集键，用于验证缓存命中

    def download_audio(self, episode: Episode, dest: Path) -> Path:
        self.downloads.append(episode.bvid)
        if self.download_error is not None:
            raise self.download_error
        dest.write_bytes(b"fake-audio-bytes")
        return dest


def _episodes(n: int) -> list[Episode]:
    return [Episode(bvid=f"BV{i:02d}", title=f"标题{i}", index=i) for i in range(1, n + 1)]


def _track(text: str) -> SubtitleTrack:
    return SubtitleTrack(lan="zh-CN", lines=[SubtitleLine(from_time=0, to_time=1, content=text)])


@pytest.fixture
def fake_transcribe(monkeypatch):
    """打桩 ensure_dependency 与 transcribe_audio，按内容映射假转写结果。"""
    monkeypatch.setattr(asr_mod, "ensure_dependency", lambda: None)

    def _transcribe(path, *, model_size="small", log=lambda s: None, model=None, proxy=None):
        return [
            SubtitleLine(from_time=0.0, to_time=1.0, content=f"转写自 {Path(path).name}。")
        ]

    monkeypatch.setattr(asr_mod, "transcribe_audio", _transcribe)
    return _transcribe


def test_no_subtitle_episode_falls_back_to_asr(tmp_path, fake_transcribe):
    client = FakeAsrClient(episodes=_episodes(2), script={1: None, 2: _track("自带字幕。")})

    outcome = run_collection("100", tmp_path, client, asr=True)

    statuses = {r.episode.index: r.status for r in outcome.results}
    assert statuses[1] == EpisodeStatus.SUCCESS  # 转写兜底成功
    assert statuses[2] == EpisodeStatus.SUCCESS
    assert outcome.asr_count == 1
    note = (tmp_path / "测试合集" / "EP01 标题1.md").read_text(encoding="utf-8")
    assert f"转写自 {state.audio_cache_path(_episodes(1)[0]).name}。" in note
    assert "本地转写 1" in summarize(outcome)


def test_asr_disabled_keeps_no_subtitle(tmp_path, fake_transcribe):
    client = FakeAsrClient(episodes=_episodes(1), script={1: None})

    outcome = run_collection("100", tmp_path, client, asr=False)

    assert outcome.results[0].status == EpisodeStatus.NO_SUBTITLE
    assert outcome.asr_count == 0


def test_asr_limit_stops_further_transcription(tmp_path, fake_transcribe):
    client = FakeAsrClient(episodes=_episodes(2), script={1: None, 2: None})

    outcome = run_collection("100", tmp_path, client, asr=True, asr_limit=1)

    statuses = {r.episode.index: r.status for r in outcome.results}
    assert statuses[1] == EpisodeStatus.SUCCESS
    assert statuses[2] == EpisodeStatus.NO_SUBTITLE
    assert outcome.asr_count == 1


def test_asr_download_failure_marks_episode_failed(tmp_path, fake_transcribe):
    client = FakeAsrClient(episodes=_episodes(1), script={1: None})
    client.download_error = PlatformError("音频下载失败（HTTP 403）")

    outcome = run_collection("100", tmp_path, client, asr=True)

    assert outcome.results[0].status == EpisodeStatus.FAILED
    assert "403" in (outcome.results[0].reason or "")


def test_client_without_download_audio_warns_and_skips(tmp_path, fake_transcribe):
    """平台不支持音频下载时：告警一次，全部保持无字幕，不中断。"""
    logs: list[str] = []
    client = FakeClient(episodes=_episodes(1), script={1: None})

    outcome = run_collection("100", tmp_path, client, asr=True, log=logs.append)

    assert outcome.results[0].status == EpisodeStatus.NO_SUBTITLE
    assert any("不支持音频下载" in line for line in logs)


def test_missing_dependency_fails_fast_before_any_download(tmp_path, monkeypatch):
    def _raise():
        raise AsrDependencyError("未安装语音转写依赖。请先执行 pip install -e \".[asr]\"")

    monkeypatch.setattr(asr_mod, "ensure_dependency", _raise)
    client = FakeAsrClient(episodes=_episodes(1), script={1: None})

    with pytest.raises(AsrDependencyError):
        run_collection("100", tmp_path, client, asr=True)


def test_transcription_empty_result_marks_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_mod, "ensure_dependency", lambda: None)
    monkeypatch.setattr(asr_mod, "transcribe_audio", lambda *a, **k: [])
    client = FakeAsrClient(episodes=_episodes(1), script={1: None})

    outcome = run_collection("100", tmp_path, client, asr=True)

    assert outcome.results[0].status == EpisodeStatus.FAILED
    assert "转写结果为空" in (outcome.results[0].reason or "")


# ---- 音频缓存 ----
def test_audio_cache_kept_on_failure_and_reused_on_rerun(tmp_path, monkeypatch):
    """转写失败的音频留在缓存里：重跑直接复用，不再下载一遍。"""
    monkeypatch.setattr(asr_mod, "ensure_dependency", lambda: None)
    monkeypatch.setattr(asr_mod, "transcribe_audio", lambda *a, **k: [])  # 空结果＝失败

    episodes = _episodes(1)
    first = FakeAsrClient(episodes=episodes, script={1: None})
    assert run_collection("100", tmp_path, first, asr=True).results[0].status == (
        EpisodeStatus.FAILED
    )
    cached = state.audio_cache_path(episodes[0])
    assert cached.is_file()  # 失败 → 留下

    monkeypatch.setattr(
        asr_mod,
        "transcribe_audio",
        lambda *a, **k: [SubtitleLine(from_time=0.0, to_time=1.0, content="转写结果。")],
    )
    logs: list[str] = []
    second = FakeAsrClient(episodes=episodes, script={1: None})
    outcome = run_collection("100", tmp_path, second, asr=True, log=logs.append)

    assert second.downloads == []  # 关键：没有重新下载
    assert any("命中缓存" in line for line in logs)
    assert outcome.results[0].status == EpisodeStatus.SUCCESS
    assert not cached.exists()  # 成功即删


def test_interrupted_download_leaves_no_poisoned_cache(tmp_path, fake_transcribe):
    """下载中途断线不能留下半截文件。

    缓存命中只判「存在且非空」，半截文件会被当成有效缓存永远不再重下 —— 转写
    必然失败，且只能手工删文件才能恢复。所以下载先写 .part 再整体换名。
    """
    class HalfwayClient(FakeAsrClient):
        def download_audio(self, episode, dest):
            self.downloads.append(episode.bvid)
            dest.write_bytes(b"half")  # 写了一半
            raise PlatformError("音频下载失败（连接中断）")

    episodes = _episodes(1)
    client = HalfwayClient(episodes=episodes, script={1: None})
    outcome = run_collection("100", tmp_path, client, asr=True)

    assert outcome.results[0].status == EpisodeStatus.FAILED
    cache_dir = state.audio_cache_root()
    assert list(cache_dir.glob("*")) == []  # 既没半截 .bin，也没留下 .part

    # 关键：重跑仍然重新下载，没有被半截文件卡死
    retry = FakeAsrClient(episodes=episodes, script={1: None})
    assert run_collection("100", tmp_path, retry, asr=True).results[0].status == (
        EpisodeStatus.SUCCESS
    )
    assert retry.downloads == ["BV01"]


def test_audio_cache_deleted_on_success(tmp_path, fake_transcribe):
    episodes = _episodes(1)
    client = FakeAsrClient(episodes=episodes, script={1: None})

    run_collection("100", tmp_path, client, asr=True)

    assert client.downloads == ["BV01"]
    assert list(state.audio_cache_root().glob("*.bin")) == []


def test_no_audio_cache_ignores_existing_file_and_leaves_nothing(tmp_path, monkeypatch):
    """--no-audio-cache：不命中已有缓存、每次都重下，失败也不留文件。"""
    monkeypatch.setattr(asr_mod, "ensure_dependency", lambda: None)
    monkeypatch.setattr(asr_mod, "transcribe_audio", lambda *a, **k: [])

    episodes = _episodes(1)
    cached = state.audio_cache_path(episodes[0])
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"stale-audio")

    client = FakeAsrClient(episodes=episodes, script={1: None})
    outcome = run_collection("100", tmp_path, client, asr=True, audio_cache=False)

    assert client.downloads == ["BV01"]  # 无视缓存，仍然下载
    assert outcome.results[0].status == EpisodeStatus.FAILED
    assert not cached.exists()  # 失败也不留
