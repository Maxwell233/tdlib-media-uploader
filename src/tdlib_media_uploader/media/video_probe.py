"""Unified video media probe and compatibility evaluation.

Provides container, codec, stream detection and compatibility classification
for Telegram Video uploads using ffprobe JSON output with safe fallbacks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from ..config.paths import APP_DATA_DIR, RESOURCE_DIR
from ..core.filesystem_legacy import display_path, run_cancellable_process

PROJECT_DIR = RESOURCE_DIR


@dataclass(frozen=True)
class VideoMediaInfo(Mapping[str, Any]):
    """Unified container and stream metadata for video files."""

    container: str
    video_codec: str
    audio_codec: str | None
    width: int
    height: int
    duration: float
    fps: float | None
    has_video_stream: bool
    compatibility: str
    has_audio_stream: bool = False
    recommended_action: str = "skip"

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __iter__(self):
        return iter(
            (
                "container",
                "video_codec",
                "audio_codec",
                "width",
                "height",
                "duration",
                "fps",
                "has_video_stream",
                "has_audio_stream",
                "compatibility",
                "recommended_action",
            )
        )

    def __len__(self) -> int:
        return 11


def _hidden_subprocess_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def _find_ffmpeg() -> str | None:
    configured = os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())

    names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg",)
    candidates = [
        PROJECT_DIR / "tools" / "ffmpeg" / names[0],
        PROJECT_DIR / "tools" / names[0],
        PROJECT_DIR / names[0],
        APP_DATA_DIR / "tools" / "ffmpeg" / names[0],
        APP_DATA_DIR / "tools" / names[0],
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    return shutil.which("ffmpeg")


def _find_ffprobe() -> str | None:
    """Find ffprobe binary following portable, environment, and system fallback order."""
    for env_var in ("TDLIB_FFPROBE_EXE", "IMAGEIO_FFPROBE_EXE"):
        configured = os.environ.get(env_var, "").strip()
        if configured and Path(configured).is_file():
            return str(Path(configured).resolve())

    ffmpeg_exe = _find_ffmpeg()
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    if ffmpeg_exe:
        sibling = Path(ffmpeg_exe).with_name(name)
        if sibling.is_file():
            return str(sibling.resolve())

    candidates = [
        PROJECT_DIR / "tools" / "ffmpeg" / name,
        PROJECT_DIR / "tools" / name,
        PROJECT_DIR / name,
        APP_DATA_DIR / "tools" / "ffmpeg" / name,
        APP_DATA_DIR / "tools" / name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())

    return shutil.which("ffprobe")


def _parse_fps(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        if "/" in str(value):
            parts = str(value).split("/", 1)
            num = float(parts[0])
            den = float(parts[1])
            if den > 0:
                fps = round(num / den, 3)
                return fps if fps > 0 else None
        else:
            fps = float(value)
            return round(fps, 3) if fps > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def _parse_duration(value: Any) -> float:
    try:
        duration = float(value or 0)
        return max(0.0, duration) if math.isfinite(duration) else 0.0
    except (TypeError, ValueError):
        return 0.0


ISO_BMFF_FORMAT_NAMES = frozenset({
    "mov",
    "mp4",
    "m4a",
    "3gp",
    "3g2",
    "mj2",
    "quicktime",
    "isom",
    "mp41",
    "mp42",
})

REMUX_FORMAT_NAMES = frozenset({
    "matroska",
    "webm",
    "avi",
    "mpegts",
})


def determine_supports_streaming(info: Any) -> bool:
    """Determine whether the video can declare supports_streaming for Telegram.

    Note: This is a Telegram metadata flag indicating streaming playback intent,
    not a verification or guarantee of faststart/moov atom placement.
    Native, remuxed, or legacy-fallback MP4, MOV, and M4V containers declare supports_streaming.
    """
    if isinstance(info, VideoMediaInfo):
        return info.compatibility in {"native", "legacy", "remux"}
    if isinstance(info, Mapping):
        return bool(info.get("supports_streaming", True))
    return True


def format_unsupported_reason(path: Path, info: VideoMediaInfo) -> str:
    """Format structured diagnostic reason for skipped videos."""
    container_label = info.container.upper() if info.container else "未知"
    video_codec_label = info.video_codec if info.video_codec else "未知"
    audio_codec_label = info.audio_codec if info.audio_codec else "无"

    if not info.has_video_stream:
        cause = "文件缺少有效视频流"
    elif info.compatibility == "invalid":
        cause = f"视频媒体属性异常（{info.width}x{info.height}，时长 {info.duration:.2f}s）或文件损坏"
    elif info.video_codec in ("unknown", ""):
        cause = "未找到 ffprobe，无法验证视频编码兼容性"
    elif info.has_audio_stream and info.audio_codec not in {"aac", "mp3"}:
        cause = f"音频编码（{audio_codec_label}）不受支持（仅支持 AAC、MP3 或无音轨），且禁止有损转码"
    elif info.compatibility == "unsupported" and info.container not in (
        "mp4", "mov", "m4v", "mkv", "avi", "ts", "mts", "m2ts"
    ):
        cause = f"文件格式（{container_label}）与扩展名不匹配或非受支持容器"
    else:
        cause = "当前编码不在直接 Telegram Video 支持范围内"

    return (
        f"{path.name}\n"
        f"容器：{container_label}\n"
        f"视频编码：{video_codec_label}\n"
        f"音频编码：{audio_codec_label}\n\n"
        f"{cause}，已跳过。"
    )


def _probe_with_ffprobe(
    file_path: Path,
    ffprobe_exe: str,
    cancel_event=None,
    timeout: float = 45.0,
) -> VideoMediaInfo:
    command = [
        ffprobe_exe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        display_path(file_path),
    ]
    process_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        **_hidden_subprocess_kwargs(),
    }
    ext = file_path.suffix.lstrip(".").lower()
    try:
        result = run_cancellable_process(
            command,
            cancel_event=cancel_event,
            **process_kwargs,
        )
    except TimeoutError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"读取视频媒体信息超时：{file_path.name}") from exc
    except Exception as exc:
        raise RuntimeError(
            f"无法读取视频媒体信息：{file_path.name}\n{type(exc).__name__}: {exc}"
        ) from exc

    if result.returncode != 0:
        return VideoMediaInfo(
            container=ext,
            video_codec="",
            audio_codec=None,
            width=0,
            height=0,
            duration=0.0,
            fps=None,
            has_video_stream=False,
            compatibility="invalid",
        )

    try:
        data = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError):
        return VideoMediaInfo(
            container=ext,
            video_codec="",
            audio_codec=None,
            width=0,
            height=0,
            duration=0.0,
            fps=None,
            has_video_stream=False,
            compatibility="invalid",
        )

    format_info = data.get("format", {})
    streams = data.get("streams", [])

    video_stream = None
    audio_stream = None
    for stream in streams:
        c_type = stream.get("codec_type")
        if c_type == "video" and video_stream is None:
            disposition = stream.get("disposition") or {}
            if disposition.get("attached_pic") != 1:
                video_stream = stream
        elif c_type == "audio" and audio_stream is None:
            audio_stream = stream

    if not video_stream:
        return VideoMediaInfo(
            container=ext,
            video_codec="",
            audio_codec=None,
            width=0,
            height=0,
            duration=0.0,
            fps=None,
            has_video_stream=False,
            has_audio_stream=audio_stream is not None,
            compatibility="invalid",
            recommended_action="error",
        )

    video_codec = str(video_stream.get("codec_name") or "").lower()
    audio_codec = str(audio_stream.get("codec_name") or "").lower() if audio_stream else None
    has_audio_stream = audio_stream is not None

    try:
        width = int(video_stream.get("width") or 0)
        height = int(video_stream.get("height") or 0)
    except (TypeError, ValueError):
        width, height = 0, 0

    duration = _parse_duration(
        video_stream.get("duration") or format_info.get("duration")
    )
    fps = _parse_fps(
        video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate")
    )

    format_name = str(format_info.get("format_name") or "").lower()
    format_names = {part.strip().lower() for part in format_name.split(",") if part.strip()}
    is_iso_bmff = bool(format_names & ISO_BMFF_FORMAT_NAMES)
    is_remux_container = bool(format_names & REMUX_FORMAT_NAMES) or (ext in {"mkv", "avi", "ts", "mts", "m2ts"})

    if is_iso_bmff and ext in {"mp4", "mov", "m4v"}:
        container = ext
    elif is_iso_bmff:
        container = "mp4" if "mp4" in format_names else ("mov" if "mov" in format_names else (ext or format_name.split(",")[0]))
    elif ext in {"mkv", "avi", "ts", "mts", "m2ts"}:
        container = ext
    else:
        container = format_name.split(",")[0] or ext

    if width <= 1 or height <= 1 or duration <= 0:
        return VideoMediaInfo(
            container=container,
            video_codec=video_codec,
            audio_codec=audio_codec,
            width=width,
            height=height,
            duration=duration,
            fps=fps,
            has_video_stream=True,
            has_audio_stream=has_audio_stream,
            compatibility="invalid",
            recommended_action="error",
        )

    # Native codecs: H.264 / AVC and H.265 / HEVC in ISO-BMFF / QuickTime container
    is_h264 = video_codec in {"h264", "avc", "avc1"}
    is_hevc = video_codec in {"hevc", "h265", "hev1", "hvc1"}
    is_native_container = is_iso_bmff and (ext in {"mp4", "mov", "m4v"})
    is_mkv = ext == "mkv" and bool(format_names & {"matroska", "webm"})
    is_avi = ext == "avi" and bool(format_names & {"avi"})
    is_ts = ext in {"ts", "mts", "m2ts"} and bool(format_names & {"mpegts"})
    is_remux_container = is_mkv or is_avi or is_ts
    is_video_compat = is_h264 or is_hevc
    is_audio_compat = (not has_audio_stream) or (audio_codec in {"aac", "mp3"})

    if is_native_container and is_video_compat and is_audio_compat:
        compatibility = "native"
        recommended_action = "upload"
    elif is_remux_container and is_video_compat and is_audio_compat:
        compatibility = "remux"
        recommended_action = "remux"
    elif container == "webm" and video_codec in {"vp8", "vp9"}:
        compatibility = "experimental"
        recommended_action = "skip"
    else:
        compatibility = "unsupported"
        recommended_action = "skip"

    return VideoMediaInfo(
        container=container,
        video_codec=video_codec,
        audio_codec=audio_codec,
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        has_video_stream=True,
        has_audio_stream=has_audio_stream,
        compatibility=compatibility,
        recommended_action=recommended_action,
    )


def _fallback_probe_without_ffprobe(
    file_path: Path,
    cancel_event=None,
    timeout: float = 45.0,
) -> VideoMediaInfo:
    """Safe fallback probe when ffprobe is absent.

    MP4 uses imageio-ffmpeg reader fallback for backward compatibility.
    MOV / M4V safely downgrade to unsupported with clear diagnosis.
    """
    ext = file_path.suffix.lstrip(".").lower()
    if ext != "mp4":
        return VideoMediaInfo(
            container=ext,
            video_codec="unknown",
            audio_codec=None,
            width=0,
            height=0,
            duration=0.0,
            fps=None,
            has_video_stream=True,
            has_audio_stream=False,
            compatibility="unsupported",
            recommended_action="skip",
        )

    try:
        import imageio_ffmpeg
        reader = imageio_ffmpeg.read_frames(display_path(file_path))
        from .legacy_video import _next_frame_metadata
        metadata = (
            _next_frame_metadata(reader, timeout=timeout)
            if cancel_event is None
            else _next_frame_metadata(reader, timeout=timeout, cancel_event=cancel_event)
        )
    except TimeoutError:
        raise
    except Exception:
        return VideoMediaInfo(
            container="mp4",
            video_codec="unknown",
            audio_codec=None,
            width=0,
            height=0,
            duration=0.0,
            fps=None,
            has_video_stream=False,
            has_audio_stream=False,
            compatibility="invalid",
            recommended_action="error",
        )
    finally:
        if "reader" in locals() and reader is not None:
            try:
                reader.close()
            except Exception:
                pass

    size = metadata.get("size") or metadata.get("source_size")
    duration = _parse_duration(metadata.get("duration"))
    fps = _parse_fps(str(metadata.get("fps") or ""))
    if not size or len(size) != 2:
        return VideoMediaInfo(
            container="mp4",
            video_codec="unknown",
            audio_codec=None,
            width=0,
            height=0,
            duration=0.0,
            fps=fps,
            has_video_stream=False,
            has_audio_stream=False,
            compatibility="invalid",
            recommended_action="error",
        )

    width, height = int(size[0]), int(size[1])
    if width <= 1 or height <= 1 or duration <= 0:
        return VideoMediaInfo(
            container="mp4",
            video_codec="unknown",
            audio_codec=None,
            width=width,
            height=height,
            duration=duration,
            fps=fps,
            has_video_stream=True,
            has_audio_stream=False,
            compatibility="invalid",
            recommended_action="error",
        )

    return VideoMediaInfo(
        container="mp4",
        video_codec="unknown",
        audio_codec=None,
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        has_video_stream=True,
        has_audio_stream=False,
        compatibility="legacy",
        recommended_action="upload",
    )


def probe_video(
    path: Path | str,
    cancel_event=None,
    timeout: float = 45.0,
) -> VideoMediaInfo:
    """Probe video file and return structured VideoMediaInfo."""
    file_path = Path(path)
    if not file_path.is_file():
        raise RuntimeError(f"视频文件不存在：{file_path}")

    ffprobe = _find_ffprobe()
    if not ffprobe:
        return _fallback_probe_without_ffprobe(
            file_path, cancel_event=cancel_event, timeout=timeout
        )

    return _probe_with_ffprobe(
        file_path, ffprobe, cancel_event=cancel_event, timeout=timeout
    )
