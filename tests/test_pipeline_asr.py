"""pipeline 的 ASR 兜底单测：无字幕 → 下载音频 → 转写 → 出笔记，全程离线打桩。"""

from __future__ import annotations

from pathlib import Path

import pytest

from subtitle_cli import asr as asr_mod
from subtitle_cli.asr import AsrDependencyError
from subtitle_cli.bilibili.models import Episode, EpisodeStatus, SubtitleLine, SubtitleTrack
from subtitle_cli.errors import PlatformError
from subtitle_cli.pipeline import run_collection, summarize

from tests.test_pipeline import FakeClient  # 复用无 download_audio 的基础假客户端


class FakeAsrClient(FakeClient):
    """在 FakeClient 上补 download_audio：把"音频内容"写到目标文件。"""

    download_error: Exception | None = None

    def download_audio(self, episode: Episode, dest: Path) -> Path:
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

    def _transcribe(path, *, model_size="small", log=lambda s: None, model=None):
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
    assert "转写自 audio-1.bin。" in note
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
