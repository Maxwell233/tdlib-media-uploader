# -*- coding: utf-8 -*-
"""Platform utilities, tool validation, and shared formatting helpers for the GUI."""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any

from PySide6.QtCore import QLibraryInfo
from PySide6.QtGui import QIcon

from ..config.paths import DATA_DIR, RESOURCE_DIR
from ..core.filesystem_legacy import run_cancellable_process

PROJECT_DIR = RESOURCE_DIR

MEDIA_KINDS = ("video", "image", "mixed")
KIND_LABELS = {"video": "视频", "image": "图片", "mixed": "混合"}
KIND_PATH_KEYS = {"video": "VIDEO_DIR", "image": "IMAGE_DIR", "mixed": "MIXED_DIR"}
KIND_PATH_CONFIG_KEYS = {"video": "video_dir", "image": "image_dir", "mixed": "mixed_dir"}
CAPTION_EDITOR_SOFT_LIMIT = 4096

ICON_NAME = "tdlib_media_uploader_icon.png" if sys.platform == "darwin" else "tdlib_media_uploader_icon.ico"
ICON_PATH = PROJECT_DIR / "assets" / ICON_NAME
WINDOWS_APP_USER_MODEL_ID = "Maxwell233.TDLibMediaUploader"


def require_kind(kind: str) -> str:
    normalized = str(kind).strip().lower()
    if normalized not in MEDIA_KINDS:
        raise ValueError(f"未知媒体类型：{kind}")
    return normalized


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(str(kind).lower(), str(kind))


def format_size(value: float | int | None) -> str:
    value = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def format_eta(seconds: float | int | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}" if hours else f"{mins:02d}:{secs:02d}"


def format_date(value: Any, fmt: str = "%Y-%m-%d %H:%M:%S", missing: str = "—") -> str:
    if not value:
        return missing
    try:
        if isinstance(value, (int, float)):
            return _dt.datetime.fromtimestamp(value).strftime(fmt)
        return str(value)
    except Exception:
        return str(value)


def path_text(value: Any) -> str:
    return str(value) if value is not None else ""


def hidden_subprocess_kwargs() -> dict[str, Any]:
    """Prevent a console window when validating tools on Windows."""
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "startupinfo": startupinfo,
    }


def validate_exiftool_path(value: str, runner=None) -> str:
    """Return a user-facing error when ExifTool cannot answer ``-ver`` quickly."""
    if runner is None:
        import sys
        main_mod = sys.modules.get("tdlib_media_uploader.gui.main_window")
        runner = getattr(main_mod, "run_cancellable_process", run_cancellable_process) if main_mod else run_cancellable_process

    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    candidate = Path(os.path.expandvars(os.path.expanduser(text)))
    if not candidate.is_absolute():
        for base in (RESOURCE_DIR, DATA_DIR, PROJECT_DIR):
            possible = base / candidate
            if possible.is_file():
                candidate = possible
                break
    if os.name == "nt" and candidate.name.casefold() == "exiftool(-k).exe":
        return "请使用 exiftool.exe。exiftool(-k).exe 会等待按键，不适合后台扫描，请先重命名后再保存。"
    if not candidate.is_file():
        return f"找不到 ExifTool：{candidate}"
    try:
        result = runner(
            [str(candidate), "-ver"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            check=False,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return "ExifTool 版本检查超时（5 秒）。请确认使用的是可直接运行的 exiftool.exe。"
    except (OSError, UnicodeError, subprocess.SubprocessError, TimeoutError) as exc:
        return f"ExifTool 无法运行：{exc}"
    if result.returncode != 0:
        detail = str(result.stderr or result.stdout or "").strip()
        return f"ExifTool 版本检查失败（退出码 {result.returncode}）" + (
            f"：{detail}" if detail else "。"
        )
    if not str(result.stdout or "").strip():
        return "ExifTool 未返回版本号，请确认路径指向可执行文件。"
    return ""


def prepare_windows_app_identity() -> None:
    """Give Windows a stable taskbar identity before any UI is created."""
    if os.name != "nt":
        return
    try:
        import ctypes

        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        set_app_id(WINDOWS_APP_USER_MODEL_ID)
    except (AttributeError, OSError, TypeError):
        pass


def prepare_qt_plugins() -> None:
    """Make bundled Qt plugins loadable on macOS installs with hidden flags."""
    if sys.platform != "darwin":
        return
    plugins_path = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
    hidden_flag = getattr(stat, "UF_HIDDEN", 0)
    if hidden_flag:
        try:
            for item in plugins_path.rglob("*"):
                try:
                    flags = item.stat().st_flags
                    if flags & hidden_flag:
                        os.chflags(item, flags & ~hidden_flag)
                except OSError:
                    continue
        except OSError:
            pass
    platforms_path = plugins_path / "platforms"
    if platforms_path.is_dir():
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(platforms_path))


def application_icon() -> QIcon:
    """Load the packaged icon with a PNG fallback for Windows taskbar shells."""
    candidates = [ICON_PATH]
    if os.name == "nt":
        candidates.append(PROJECT_DIR / "assets" / "tdlib_media_uploader_icon.png")
    for path in candidates:
        if not path.is_file():
            continue
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    return QIcon()


# Compatibility aliases for legacy private naming
_require_kind = require_kind
_kind_label = kind_label
_fmt_size = format_size
_fmt_eta = format_eta
_fmt_date = format_date
_path_text = path_text
_hidden_subprocess_kwargs = hidden_subprocess_kwargs
_validate_exiftool_path = validate_exiftool_path
_prepare_windows_app_identity = prepare_windows_app_identity
_prepare_qt_plugins = prepare_qt_plugins
_application_icon = application_icon


__all__ = [
    "CAPTION_EDITOR_SOFT_LIMIT",
    "ICON_NAME",
    "ICON_PATH",
    "KIND_LABELS",
    "KIND_PATH_CONFIG_KEYS",
    "KIND_PATH_KEYS",
    "MEDIA_KINDS",
    "PROJECT_DIR",
    "WINDOWS_APP_USER_MODEL_ID",
    "_application_icon",
    "_fmt_date",
    "_fmt_eta",
    "_fmt_size",
    "_hidden_subprocess_kwargs",
    "_kind_label",
    "_path_text",
    "_prepare_qt_plugins",
    "_prepare_windows_app_identity",
    "_require_kind",
    "_validate_exiftool_path",
    "application_icon",
    "format_date",
    "format_eta",
    "format_size",
    "hidden_subprocess_kwargs",
    "kind_label",
    "path_text",
    "prepare_qt_plugins",
    "prepare_windows_app_identity",
    "require_kind",
    "validate_exiftool_path",
]
