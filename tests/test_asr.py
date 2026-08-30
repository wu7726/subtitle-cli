"""asr 模块单测：依赖检查、转写结果映射、模型缓存，全部离线（假模型注入）。"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from subtitle_cli import asr
from subtitle_cli.asr import AsrDependencyError, ensure_dependency, segment_lines, transcribe_audio


def _fake_model(segments, language="zh"):
    def transcribe(path, vad_filter=True):
        return iter(segments), SimpleNamespace(language=language)

    return SimpleNamespace(transcribe=transcribe)


def test_dependency_missing_gives_install_hint(monkeypatch):
    """faster_whisper 不可用时给出可执行的安装指引。"""
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import 即抛 ImportError
    with pytest.raises(AsrDependencyError) as exc:
        ensure_dependency()
    assert "pip install" in str(exc.value)
    assert "asr" in str(exc.value)


def test_segment_lines_maps_tuples():
    lines = segment_lines([(0.0, 1.5, " 第一句。"), (2.0, 3.0, "  "), (3.5, 4.0, "第二句。")])
    assert [(l.from_time, l.to_time, l.content) for l in lines] == [
        (0.0, 1.5, "第一句。"),
        (3.5, 4.0, "第二句。"),
    ]


def test_transcribe_maps_segments_and_logs(tmp_path):
    audio = tmp_path / "a.bin"
    audio.write_bytes(b"fake")
    logs: list[str] = []
    model = _fake_model(
        [SimpleNamespace(start=0.0, end=1.0, text=" 你好。"), SimpleNamespace(start=2.0, end=3.0, text="世界。")],
        language="zh",
    )
    lines = transcribe_audio(audio, model=model, log=logs.append)
    assert [l.content for l in lines] == ["你好。", "世界。"]
    assert lines[0].from_time == 0.0 and lines[1].to_time == 3.0
    assert any("识别语言：zh" in line for line in logs)


def test_transcribe_progress_log_every_n_segments(tmp_path, monkeypatch):
    monkeypatch.setattr("subtitle_cli.config.ASR_LOG_EVERY_SEGMENTS", 2)
    audio = tmp_path / "a.bin"
    audio.write_bytes(b"fake")
    logs: list[str] = []
    segs = [
        SimpleNamespace(start=float(i), end=float(i + 1), text=f"第{i}句。") for i in range(5)
    ]
    lines = transcribe_audio(audio, model=_fake_model(segs), log=logs.append)
    assert len(lines) == 5
    assert any("已转写 2 段" in line for line in logs)
    assert any("已转写 4 段" in line for line in logs)
    assert not any("已转写 5 段" in line for line in logs)


def test_transcribe_skips_empty_text(tmp_path):
    audio = tmp_path / "a.bin"
    audio.write_bytes(b"fake")
    model = _fake_model([SimpleNamespace(start=0.0, end=1.0, text="   ")])
    assert transcribe_audio(audio, model=model) == []
