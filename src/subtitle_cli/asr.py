"""本地语音转写（faster-whisper）：无现成字幕/文稿时的兜底。

依赖是可选的（pyproject 的 [asr] extra）：未安装时给出可执行的安装指引，
不影响工具其余功能。模型按进程缓存（加载一次，多集复用）。
转写结果映射为统一 SubtitleLine，交回既有 converter/reviewer 管线。

模型下载源：ModelScope（国内直连，约 5MB/s，支持断点续传）优先，
失败回落 HuggingFace（默认 hf-mirror.com 镜像，国内环境不保证可用）。
"""

from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Callable, Iterator

import httpx

from . import config
from .bilibili.models import SubtitleLine

# huggingface_hub 1.x 默认走 Xet 存储协议，hf-mirror 不支持，必须禁用；
# symlinks 警告在 Windows 上无意义。用户已设置的环境变量不会被覆盖。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# ---- CUDA 运行库路径注入（必须在 ctranslate2 被 import 之前完成）----
#
# pip 安装的 nvidia-cublas-cu12 / nvidia-cudnn-cu12 把 DLL 放在
# site-packages/nvidia/<lib>/bin，ctranslate2 自带的 cudnn 放在自己包目录，
# 两者都不在系统 PATH 上。若不显式注入 DLL 搜索路径，ctranslate2 会在
# 推理时抛 "Library cublas64_12.dll is not found or cannot be loaded"；
# 更麻烦的是 ctranslate2.dll 一旦加载，其依赖搜索路径即被固化，
# 事后再注入也无效（表现为推理阶段直接挂死），因此必须在模块导入期完成。

_CUDA_DLL_SUBDIRS = (
    "nvidia/cublas/bin",
    "nvidia/cudnn/bin",
    "nvidia/cuda_nvrtc/bin",
    "ctranslate2",
)
_CUBLAS_NAMES = ("cublas64_12.dll", "cublas64_11.dll")
_CUDNN_NAMES = ("cudnn64_9.dll", "cudnn64_8.dll")

# os.add_dll_directory 返回的句柄一旦被 GC，目录会立即移出搜索路径，必须保活。
_DLL_HANDLES: list[object] = []
_DLL_DIRS_REGISTERED = False


def _cuda_dll_dirs() -> list[Path]:
    """CUDA 运行库所在目录（按当前解释器的 site-packages 定位，去重）。"""
    import sysconfig

    dirs: list[Path] = []
    seen: set[str] = set()
    for key in ("purelib", "platlib"):
        base = sysconfig.get_paths().get(key)
        if not base:
            continue
        for sub in _CUDA_DLL_SUBDIRS:
            candidate = Path(base) / sub
            if candidate.is_dir() and str(candidate) not in seen:
                seen.add(str(candidate))
                dirs.append(candidate)
    return dirs


def _register_cuda_dll_dirs() -> list[Path]:
    """把 CUDA 运行库目录注入 DLL 搜索路径（仅 Windows，幂等）。"""
    global _DLL_DIRS_REGISTERED
    dirs = _cuda_dll_dirs()
    if os.name == "nt" and not _DLL_DIRS_REGISTERED:
        for directory in dirs:
            try:
                _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
            except OSError:
                continue
        _DLL_DIRS_REGISTERED = True
    return dirs


def _cuda_runtime_usable(dll_dirs: list[Path]) -> bool:
    """显卡推理所需的运行库（cuBLAS + cuDNN）是否齐备。

    按文件存在性判断，而不是 ctypes.WinDLL(name)：后者走系统 PATH 解析，
    而 pip 装的这些 DLL 恰恰不在 PATH 上，会把可用的显卡误判成缺失。
    非 Windows 交由转写时的运行时错误回退。
    """
    if os.name != "nt":
        return True

    def has_any(names: tuple[str, ...]) -> bool:
        return any((directory / name).is_file() for directory in dll_dirs for name in names)

    return has_any(_CUBLAS_NAMES) and has_any(_CUDNN_NAMES)


# 注入结果缓存：模块导入期算一次，后续设备判定直接复用
_CUDA_DLL_DIRS: list[Path] = _register_cuda_dll_dirs()

INSTALL_HINT = (
    "未安装语音转写依赖。请先执行：pip install -e \".[asr]\""
    "（首次转写会自动下载模型到 " + config.ASR_MODEL_DIR + "）"
)

MODELSCOPE_URL = "https://www.modelscope.cn/models/gpustack/faster-whisper-{size}/resolve/master/{file}"
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")

# 进程级模型缓存：同一模型只加载一次（加载耗时数秒，逐集转写时必须复用）
_MODELS: dict[str, object] = {}


class AsrDependencyError(RuntimeError):
    """faster-whisper 未安装。"""


class AsrModelError(RuntimeError):
    """模型获取失败（ModelScope 与 HuggingFace 均不可用）。"""


def ensure_dependency() -> None:
    """检查转写依赖是否可用；缺失时抛带安装指引的错误。"""
    try:
        import faster_whisper  # noqa: F401
    except ImportError as exc:
        raise AsrDependencyError(INSTALL_HINT) from exc


def model_dir(model_size: str) -> Path:
    """某模型的本地目录：<ASR_MODEL_DIR>/faster-whisper-<size>。"""
    return Path(config.ASR_MODEL_DIR).expanduser() / f"faster-whisper-{model_size}"


def model_complete(directory: Path) -> bool:
    """模型文件是否齐全（加载的最小集：权重 + 配置 + 词表）。"""
    return (directory / "model.bin").is_file() and (directory / "config.json").is_file() and (
        (directory / "tokenizer.json").is_file() or (directory / "vocabulary.txt").is_file()
    )


def download_model(model_size: str, *, log: Callable[[str], None] = lambda line: None) -> Path:
    """确保模型文件齐全：本地已缓存 → 直接用；否则 ModelScope 下载，
    失败回落 HuggingFace。返回模型目录。"""
    directory = model_dir(model_size)
    if model_complete(directory):
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    try:
        _download_from_modelscope(model_size, directory, log)
        if model_complete(directory):
            return directory
        raise AsrModelError(f"ModelScope 下载不完整：{directory}")
    except (AsrModelError, httpx.HTTPError, OSError) as exc:
        log(f"ModelScope 下载失败（{exc}），改用 HuggingFace（{os.environ.get('HF_ENDPOINT')}）…")
        _download_from_huggingface(model_size, directory, log)
        if model_complete(directory):
            return directory
        raise AsrModelError(f"HuggingFace 下载后模型仍不完整：{directory}") from exc


def _download_from_modelscope(model_size: str, directory: Path, log) -> None:
    for filename in MODEL_FILES:
        target = directory / filename
        if target.exists():
            continue
        url = MODELSCOPE_URL.format(size=model_size, file=filename)
        log(f"下载模型文件：{filename}（ModelScope）")
        _download_file_resumable(url, target)


def _download_from_huggingface(model_size: str, directory: Path, log) -> None:
    try:
        from faster_whisper.utils import download_model as hf_download
    except ImportError as exc:
        raise AsrModelError(INSTALL_HINT) from exc
    # faster-whisper 的 download_model 返回其缓存目录；再拷齐缺失文件
    hf_dir = Path(hf_download(model_size))
    for filename in MODEL_FILES:
        target = directory / filename
        if target.exists():
            continue
        source = hf_dir / filename
        if source.exists():
            target.write_bytes(source.read_bytes())


def _download_file_resumable(url: str, target: Path, *, attempts: int = 3) -> None:
    """流式下载到 .part 临时文件，支持断点续传，完成后原子替换。"""
    partial = target.with_suffix(target.suffix + ".part")
    last_error: Exception | None = None
    for _ in range(attempts):
        headers = {}
        mode = "wb"
        if partial.exists() and partial.stat().st_size > 0:
            headers["Range"] = f"bytes={partial.stat().st_size}-"
            mode = "ab"
        try:
            with httpx.stream("GET", url, headers=headers, timeout=60, follow_redirects=True) as resp:
                if resp.status_code == 206:
                    pass  # 续传成功
                elif resp.status_code == 200:
                    mode = "wb"  # 服务端不支持续传，从头下
                else:
                    raise AsrModelError(f"HTTP {resp.status_code}：{url}")
                with open(partial, mode) as f:
                    for chunk in resp.iter_bytes(1 << 20):
                        f.write(chunk)
            partial.replace(target)
            return
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            continue
    raise AsrModelError(f"下载失败（已重试 {attempts} 次）：{url}，{last_error}")


def _is_cuda_runtime_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(keyword in message for keyword in ("cublas", "cudnn", "cuda"))


def _compute_type_for(device: str) -> str:
    """device → compute_type；显式指定，不依赖 faster-whisper 的默认推断。"""
    configured = (config.ASR_COMPUTE_TYPE or "auto").strip().lower()
    if configured and configured != "auto":
        return configured
    return "float16" if device == "cuda" else "int8"


def _resolve_device(force_cpu: bool, log: Callable[[str], None] | None) -> str:
    """决定实际使用的设备：显式 cpu、显式 cuda，或 auto 探测。"""
    if force_cpu:
        return "cpu"
    if config.ASR_DEVICE != "auto":
        return config.ASR_DEVICE
    if _cuda_runtime_usable(_CUDA_DLL_DIRS):
        return "cuda"
    if log:
        log(
            "⚠️ 未检测到可用的 CUDA 运行库（cuBLAS/cuDNN），改用 CPU 转写；"
            "启用显卡加速：pip install nvidia-cublas-cu12 nvidia-cudnn-cu12"
        )
    return "cpu"


def _load_model(
    model_size: str,
    *,
    log: Callable[[str], None] | None = None,
    force_cpu: bool = False,
):
    """加载（或取缓存）Whisper 模型。auto：有可用 CUDA 用显卡，否则 CPU。

    按 (模型, 设备, 计算精度) 三元组缓存——加载一次，逐集复用。
    """
    device = _resolve_device(force_cpu, log)
    compute_type = _compute_type_for(device)
    key = f"{model_size}#{device}#{compute_type}"
    if key in _MODELS:
        return _MODELS[key]
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise AsrDependencyError(INSTALL_HINT) from exc
    if log:
        log(f"加载语音转写模型 {model_size}（device={device}, compute_type={compute_type}）")
    directory = download_model(model_size, log=log or (lambda line: None))
    model = WhisperModel(str(directory), device=device, compute_type=compute_type)
    _MODELS[key] = model
    return model


def _transcribe_kwargs() -> dict[str, object]:
    """集中转写参数：VAD 过滤静音、束宽、语言、跨段上下文。"""
    return {
        "vad_filter": True,
        "beam_size": config.ASR_BEAM_SIZE,
        # 空字符串 → None，交给 whisper 自动检测语种
        "language": config.ASR_LANGUAGE or None,
        "condition_on_previous_text": config.ASR_CONDITION_ON_PREVIOUS_TEXT,
    }


def transcribe_audio(
    audio_path: str | Path,
    *,
    model_size: str = config.ASR_MODEL_SIZE,
    log: Callable[[str], None] = lambda line: None,
    model: object | None = None,
) -> list[SubtitleLine]:
    """转写音频文件为字幕行。

    - VAD 过滤静音段，跳过无人声区间；
    - 语言默认自动检测（中文内容即输出简体中文）；
    - model 参数供测试注入假模型，生产路径走 _load_model 缓存。
    """
    whisper_model = model or _load_model(model_size, log=log)
    kwargs = _transcribe_kwargs()
    try:
        segments_iter, info = whisper_model.transcribe(str(audio_path), **kwargs)
    except RuntimeError as exc:
        # 语言检测在 transcribe() 调用内即时执行，CUDA 缺库会在此暴露：
        # 换 CPU 模型重试一次（仅生产路径，注入的假模型不重试）
        if model is None and _is_cuda_runtime_error(exc):
            if log:
                log("⚠️ CUDA 运行库不可用，改用 CPU 转写")
            whisper_model = _load_model(model_size, log=log, force_cpu=True)
            segments_iter, info = whisper_model.transcribe(str(audio_path), **kwargs)
        else:
            raise
    if log:
        detected = getattr(info, "language", None)
        if detected:
            log(f"识别语言：{detected}，开始转写")
    lines: list[SubtitleLine] = []
    count = 0
    for seg in segments_iter:  # 生成器：真正的转写计算在此循环中进行
        text = (seg.text or "").strip()
        if not text:
            continue
        lines.append(SubtitleLine(from_time=seg.start, to_time=seg.end, content=text))
        count += 1
        if log and count % config.ASR_LOG_EVERY_SEGMENTS == 0:
            log(f"已转写 {count} 段（进行到 {seg.end:.0f} 秒）")
    return lines


def segment_lines(raw_segments: Iterator) -> list[SubtitleLine]:
    """(start, end, text) 三元组 → SubtitleLine（测试辅助，纯函数）。"""
    return [
        SubtitleLine(from_time=float(start), to_time=float(end), content=str(text).strip())
        for start, end, text in raw_segments
        if str(text).strip()
    ]
