"""asr 模块单测：依赖检查、转写结果映射、模型缓存，全部离线（假模型注入）。"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from subtitle_cli import asr
from subtitle_cli.asr import AsrDependencyError, ensure_dependency, segment_lines, transcribe_audio


def _fake_model(segments, language="zh", captured=None):
    def transcribe(path, **kwargs):
        if captured is not None:
            captured.update(kwargs)
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
    assert [(line.from_time, line.to_time, line.content) for line in lines] == [
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
    assert [line.content for line in lines] == ["你好。", "世界。"]
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


def test_transcribe_passes_tuning_kwargs(tmp_path, monkeypatch):
    """转写参数取自 config；language 空串转 None 表示自动检测。"""
    monkeypatch.setattr("subtitle_cli.config.ASR_BEAM_SIZE", 3)
    monkeypatch.setattr("subtitle_cli.config.ASR_LANGUAGE", "")
    monkeypatch.setattr("subtitle_cli.config.ASR_CONDITION_ON_PREVIOUS_TEXT", False)
    captured: dict = {}
    model = _fake_model([SimpleNamespace(start=0.0, end=1.0, text="你好。")], captured=captured)
    transcribe_audio(tmp_path / "a.bin", model=model)
    assert captured == {
        "vad_filter": True,
        "beam_size": 3,
        "language": None,
        "condition_on_previous_text": False,
    }


def test_transcribe_passes_language_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr("subtitle_cli.config.ASR_LANGUAGE", "zh")
    captured: dict = {}
    model = _fake_model([SimpleNamespace(start=0.0, end=1.0, text="你好。")], captured=captured)
    transcribe_audio(tmp_path / "a.bin", model=model)
    assert captured["language"] == "zh"


# ---- CUDA 运行库定位与设备判定 ------------------------------------------


def test_cuda_dll_dirs_locates_nvidia_and_ctranslate2(tmp_path, monkeypatch):
    """按 site-packages 布局定位 cuBLAS/cuDNN/ctranslate2 目录，并去重。"""
    sp = tmp_path / "site-packages"
    for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin", "ctranslate2"):
        (sp / sub).mkdir(parents=True)
    monkeypatch.setattr("sysconfig.get_paths", lambda: {"purelib": str(sp), "platlib": str(sp)})
    dirs = asr._cuda_dll_dirs()
    assert sp / "nvidia" / "cublas" / "bin" in dirs
    assert sp / "nvidia" / "cudnn" / "bin" in dirs
    assert sp / "nvidia" / "cuda_nvrtc" / "bin" in dirs
    assert sp / "ctranslate2" in dirs
    assert len(dirs) == len(set(map(str, dirs)))


def test_cuda_dll_dirs_skips_missing_dirs(tmp_path, monkeypatch):
    sp = tmp_path / "sp"
    (sp / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    monkeypatch.setattr("sysconfig.get_paths", lambda: {"purelib": str(sp), "platlib": str(sp)})
    assert asr._cuda_dll_dirs() == [sp / "nvidia" / "cublas" / "bin"]


def test_cuda_runtime_usable_checks_files_not_path(tmp_path, monkeypatch):
    """按文件存在性判断：DLL 不在系统 PATH 上也不该被判成缺失。"""
    monkeypatch.setattr(asr.os, "name", "nt")
    assert asr._cuda_runtime_usable([tmp_path]) is False
    (tmp_path / "cublas64_12.dll").write_bytes(b"x")
    assert asr._cuda_runtime_usable([tmp_path]) is False  # 还缺 cuDNN
    (tmp_path / "cudnn64_9.dll").write_bytes(b"x")
    assert asr._cuda_runtime_usable([tmp_path]) is True


def test_cuda_runtime_usable_accepts_legacy_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(asr.os, "name", "nt")
    (tmp_path / "cublas64_11.dll").write_bytes(b"x")
    (tmp_path / "cudnn64_8.dll").write_bytes(b"x")
    assert asr._cuda_runtime_usable([tmp_path]) is True


def test_compute_type_follows_device(monkeypatch):
    monkeypatch.setattr("subtitle_cli.config.ASR_COMPUTE_TYPE", "auto")
    assert asr._compute_type_for("cuda") == "float16"
    assert asr._compute_type_for("cpu") == "int8"


def test_compute_type_explicit_override(monkeypatch):
    monkeypatch.setattr("subtitle_cli.config.ASR_COMPUTE_TYPE", "int8_float16")
    assert asr._compute_type_for("cuda") == "int8_float16"
    assert asr._compute_type_for("cpu") == "int8_float16"


def test_resolve_device_uses_cuda_when_runtime_ready(monkeypatch):
    monkeypatch.setattr("subtitle_cli.config.ASR_DEVICE", "auto")
    monkeypatch.setattr(asr, "_cuda_runtime_usable", lambda dirs: True)
    assert asr._resolve_device(False, None) == "cuda"


def test_resolve_device_falls_back_to_cpu_with_hint(monkeypatch):
    monkeypatch.setattr("subtitle_cli.config.ASR_DEVICE", "auto")
    monkeypatch.setattr(asr, "_cuda_runtime_usable", lambda dirs: False)
    logs: list[str] = []
    assert asr._resolve_device(False, logs.append) == "cpu"
    assert any("CUDA" in line and "nvidia-cublas" in line for line in logs)


def test_resolve_device_force_cpu_wins(monkeypatch):
    monkeypatch.setattr("subtitle_cli.config.ASR_DEVICE", "cuda")
    assert asr._resolve_device(True, None) == "cpu"


def test_resolve_device_honours_explicit_setting(monkeypatch):
    monkeypatch.setattr("subtitle_cli.config.ASR_DEVICE", "cpu")
    assert asr._resolve_device(False, None) == "cpu"
