Warning: truncated output (original token count: 42925)
Total output lines: 3893

# -*- coding: utf-8 -*-
"""PySide6 desktop interface for TDLib Media Uploader.

The GUI is the only user-facing interface.  Upload cores remain the source of
truth for scanning, Album creation, TDLib requests and resumable state.
"""

from __future__ import annotations

import datetime as _dt
import functools
import importlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import tomllib
from collections import defaultdict
from pathlib import Path

from album_metadata import (
    CaptionLimitError,
    CaptionStore,
    album_key,
    compose_caption,
    validate_caption,
    with_filename_description,
)
from app_logging import APP_LOG_PATH, LOG_DIR, TDLIB_LOG_PATH, write_app_log, write_exception
from path_utils import (
    file_mtime,
    is_link_or_junction,
    iter_files,
    media_path_sort,
    retry_fs_operation,
    stable_path,
    iter_directory_entries_with_retry,
    run_cancellable_process,
    validate_scan_root,
)
from PySide6.QtCore import QLibraryInfo, QObject, QThread, QTimer, Signal, Slot, Qt, QLockFile
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from runtime_paths import (
    APP_DATA_DIR, CONFIG_PATH, RESOURCE_DIR, TEMPLATE_CONFIG_PATH,
    DATA_DIR, VIDEO_STATE_DIR, IMAGE_STATE_DIR, MIXED_STATE_DIR,
    CAPTIONS_DIR, UPLOAD_INFLIGHT_DIR, THUMBNAIL_CACHE_DIR,
    IMAGE_COMPRESSION_CACHE_DIR, STAGING_CACHE_DIR,
    HISTORY_PATH as RUNTIME_HISTORY_PATH,
    TDLIB_DATABASE_DIR as RUNTIME_TDLIB_DATABASE_DIR,
    TDLIB_FILES_DIR as RUNTIME_TDLIB_FILES_DIR,
    read_version, ensure_data_dirs,
)
from instance_lock import InstanceLock
from self_test import run_self_test


PROJECT_DIR = RESOURCE_DIR
APP_VERSION = read_version()
MEDIA_KINDS = ("video", "image", "mixed")
KIND_LABELS = {"video": "视频", "image": "图片", "mixed": "混合"}
# The editor is normally used before a Telegram session is opened, so its
# local validation is a soft ceiling.  Each uploader revalidates against
# TDLib's ``message_caption_length_max`` immediately before sending.
CAPTION_EDITOR_SOFT_LIMIT = 4096
KIND_PATH_KEYS = {"video": "VIDEO_DIR", "image": "IMAGE_DIR", "mixed": "MIXED_DIR"}
KIND_PATH_CONFIG_KEYS = {"video": "video_dir", "image": "image_dir", "mixed": "mixed_dir"}
# The dependency-free preview path still keeps the scanner's one-stat
# snapshots so its Album keys cannot drift if a network share changes before
# the preview is rendered.
BASIC_SCAN_SNAPSHOTS: dict[str, tuple[int, int]] = {}


def _require_kind(kind: str) -> str:
    normalized = str(kind).strip().lower()
    if normalized not in MEDIA_KINDS:
        raise ValueError(f"未知媒体类型：{kind}")
    return normalized


def _kind_label(kind: str) -> str:
    return KIND_LABELS.get(str(kind).lower(), str(kind))
ICON_NAME = "tdlib_media_uploader_icon.png" if sys.platform == "darwin" else "tdlib_media_uploader_icon.ico"
ICON_PATH = PROJECT_DIR / "assets" / ICON_NAME
WINDOWS_APP_USER_MODEL_ID = "Maxwell233.TDLibMediaUploader"


def _prepare_windows_app_identity() -> None:
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
        # The GUI and executable icon still work if an older Windows shell or
        # a restricted runtime does not expose this optional API.
        pass


def _prepare_qt_plugins() -> None:
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


def _application_icon() -> QIcon:
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


_prepare_qt_plugins()


def _ensure_config_file() -> bool:
    # The packaged health check must work on a clean install without writing
    # a user config.  ``app_config`` loads the bundled template in memory for
    # this command-line mode.
    if "--self-test" in sys.argv[1:]:
        return False
    if CONFIG_PATH.exists() or not TEMPLATE_CONFIG_PATH.exists():
        return False
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEMPLATE_CONFIG_PATH, CONFIG_PATH)
    return True


_CONFIG_CREATED = _ensure_config_file()
_CONFIG_ERROR = ""
try:
    import app_config as cfg
except Exception as exc:  # The settings page can still explain the problem.
    cfg = None
    _CONFIG_ERROR = str(exc)


def _reload_config() -> str:
    global cfg, _CONFIG_ERROR
    try:
        if "app_config" in sys.modules:
            cfg = importlib.reload(sys.modules["app_config"])
        else:
            cfg = importlib.import_module("app_config")
        _CONFIG_ERROR = ""
        return ""
    except Exception as exc:
        cfg = None
        _CONFIG_ERROR = str(exc)
        return _CONFIG_ERROR


def _cfg(name: str, default=None):
    return getattr(cfg, name, default) if cfg is not None else default


def _target_for(kind: str) -> dict:
    normalized = _require_kind(kind)
    if cfg is not None and callable(getattr(cfg, "target_for", None)):
        return cfg.target_for(normalized)
    return {
        "target_mode": "forum_topic",
        "group_chat_id": _cfg("GROUP_CHAT_ID", _cfg("CHAT_ID", 0)),
        "channel_chat_id": _cfg("CHANNEL_CHAT_ID", 0),
        "forum_topic_id": _cfg("FORUM_TOPIC_ID", 0),
        "chat_id": _cfg("CHAT_ID", 0),
    }


def _fmt_size(value: float | int | None) -> str:
    value = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def _fmt_eta(seconds: float | int | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def _fmt_date(value, fmt: str = "%Y-%m-%d %H:%M:%S", missing: str = "—") -> str:
    if value is None:
        return missing
    formatter = getattr(value, "strftime", None)
    if callable(formatter):
        return formatter(fmt)
    return str(value)


def _path_text(value) -> str:
    return str(value) if value is not None else ""


def _basic_paths(kind: str, cancel_event=None) -> list[Path]:
    kind = str(kind).strip().lower()
    if kind not in MEDIA_KINDS:
        raise ValueError(f"未知媒体类型：{kind}")
    path_key = KIND_PATH_KEYS[kind]
    extension_key = {
        "video": "VIDEO_EXTENSIONS",
        "image": "IMAGE_EXTENSIONS",
        "mixed": "MIXED_EXTENSIONS",
    }[kind]
    root = Path(_cfg(path_key, PROJECT_DIR))
    extensions = set(_cfg(extension_key, set()))
    BASIC_SCAN_SNAPSHOTS.clear()
    try:
        root = validate_scan_root(
            root,
            attempts=_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
            initial_delay=_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            max_delay=_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
            cancel_event=cancel_event,
        )
    except TimeoutError:
        return []
    if kind == "mixed":
        groups, _ignored, _errors, _warnings, _skips = _basic_mixed_scan(
            root, cancel_event=cancel_event
        )
        return [item["path"] for group in groups for item in group["items"]]
    scan_result = iter_files(
        root,
        extensions,
        cancel_event=cancel_event,
        discovery_attempts=_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
        discovery_initial_delay=_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
        discovery_max_delay=_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
    )
    paths = scan_result.paths
    BASIC_SCAN_SNAPSHOTS.update({
        key: snapshot.as_tuple()
        for key, snapshot in getattr(scan_result, "snapshots", {}).items()
    })
    if kind == "image":
        mode = "mtime" if _cfg("IMAGE_SORT_MODE", "mtime") == "mtime" else "name"
    else:
        mode = (
            "mtime"
            if _cfg("VIDEO_READ_DATES", True)
            and _cfg("VIDEO_SORT_MODE", "mtime") == "mtime"
            else "name"
        )
    return media_path_sort(paths, root, mode=mode)


@functools.lru_cache(maxsize=32768)
def _path_size(path_str: str) -> int:
    try:
        return int(os.stat(path_str).st_size)
    except OSError:
        return 0

def _item_size(item) -> int:
    path = item["path"] if isinstance(item, dict) else item
    return _path_size(str(path))


def _apply_size_limits(paths: list[Path], kind: str) -> tuple[list[Path], list[dict]]:
    """Apply Telegram size limits for the GUI fallback scanner."""

    if kind not in {"video", "image", "mixed"}:
        raise ValueError(f"不支持基础扫描类型：{kind}")
    accepted = []
    skipped = []
    for path in paths:
        if kind == "mixed":
            suffix = path.suffix.lower()
            media_kind = (
                "video"
                if suffix in set(_cfg("MIXED_VIDEO_EXTENSIONS", set()))
                else "image"
            )
        else:
            media_kind = kind
        limit_key = "VIDEO_MAX_BYTES" if media_kind == "video" else "IMAGE_MAX_BYTES"
        default_limit = int(
            _cfg("VIDEO_MAX_BYTES", 4_194_304_000)
            if media_kind == "video"
            else _cfg("IMAGE_MAX_BYTES", 10 * 1024 ** 2)
        )
        limit = int(_cfg(limit_key, default_limit))
        compress_images = media_kind == "image" and bool(_cfg("IMAGE_COMPRESS_OVERSIZE", False))
        media_label = "视频" if media_kind == "video" else "Photo"
        if media_kind == "video":
            premium_limit = int(_cfg("VIDEO_PREMIUM_MAX_BYTES", 8000 * 524_288))
            standard_limit = int(_cfg("VIDEO_STANDARD_MAX_BYTES", 4000 * 524_288))
            limit_label = "约 4 GB" if limit == premium_limit else "约 2 GB" if limit == standard_limit else _fmt_size(limit)
        else:
            limit_label = _fmt_size(limit)
        snapshot = BASIC_SCAN_SNAPSHOTS.get(stable_path(path))
        size = int(snapshot[0]) if snapshot is not None else _path_size(str(path))
        if size > limit:
            action = "compress" if compress_images else "skip"
            skipped.append({
                "path": path,
                "media_kind": media_kind,
                "size": size,
                "limit": limit,
                "category": "size",
                "action": action,
                "reason": (
                    f"文件大小 {_fmt_size(size)} 超过 Telegram "
                    f"{media_label} 上限 {limit_label}"
                ),
            })
            if action == "skip":
                continue
        accepted.append(path)
    return accepted, skipped


def _basic_mixed_scan(
    root: Path, cancel_event=None
) -> tuple[list[dict], list[Path], list[str], list[str], list[dict]]:
    """Build a dependency-free mixed preview with the same group rules."""

    root = validate_scan_root(
        root,
        attempts=_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
        initial_delay=_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
        max_delay=_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
        cancel_event=cancel_event,
    )
    image_extensions = set(_cfg("IMAGE_EXTENSIONS", set()))
    video_extensions = set(_cfg("VIDEO_EXTENSIONS", set()))
    overlap = image_extensions & video_extensions
    if overlap:
        values = ", ".join(sorted(overlap))
        raise RuntimeError(f"图片和视频扩展名不能重复：{values}")
    accepted_extensions = image_extensions | video_extensions
    directories = []
    ignored = []
    errors = []
    warnings = []
    try:
        for entry in iter_directory_entries_with_retry(
            root,
            attempts=_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
            initial_delay=_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            max_delay=_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
            cancel_event=cancel_event,
        ):
            if cancel_event is not None and cancel_event.is_set():
                warnings.append("目录扫描已取消")
                break
            try:
                path = Path(entry.path)
                info = retry_fs_operation(
                    lambda entry=entry: entry.stat(follow_symlinks=False),
                    attempts=_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
                    initial_delay=_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
                    max_delay=_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
                    cancel_event=cancel_event,
                )
                if is_link_or_junction(entry):
                    warnings.append(f"跳过符号链接或 junction：{entry.path}")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    directories.append(path)
                elif stat.S_ISREG(info.st_mode) and path.suffix.lower() in accepted_extensions:
                    ignored.append(path)
            except OSError as exc:
                errors.append(f"{entry.path}: {exc}")
    except TimeoutError:
        warnings.append("目录扫描已取消")
    except OSError as exc:
        errors.append(f"{root}: {exc}")
    groups = []
    size_skips = []
    for group_path in media_path_sort(directories, root, mode="name"):
        scan_result = iter_files(
            group_path,
            accepted_extensions,
            cancel_event=cancel_event,
            discovery_attempts=_cfg("SCAN_DISCOVERY_ATTEMPTS", 3),
            discovery_initial_delay=_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15),
            discovery_max_delay=_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0),
        )
        errors.extend(scan_result.errors)
        warnings.extend(scan_result.warnings)
        if scan_result.cancelled:
            warnings.append("目录扫描已取消")
        BASIC_SCAN_SNAPSHOTS.update({
            key: snapshot.as_tuple()
            for key, snapshot in getattr(scan_result, "snapshots", {}).items()
        })
        accepted, skipped = _apply_size_limits(scan_result.paths, "mixed")
        size_skips.extend(skipped)
        media_items = []
        for path in accepted:
            suffix = path.suffix.lower()
            media_kind = "video" if suffix in video_extensions else "image"
            media_items.append({
                "path": path,
                "media_kind": media_kind,
                "group_name": group_path.name,
                **(
                    {
                        "scan_size": BASIC_SCAN_SNAPSHOTS[stable_path(path)][0],
                        "scan_mtime_ns": BASIC_SCAN_SNAPSHOTS[stable_path(path)][1],
                    }
                    if stable_path(path) in BASIC_SCAN_SNAPSHOTS
                    else {}
                ),
            })
        sort_mode = str(_cfg("MIXED_SORT_MODE", "name")).strip().lower()
        media_items = media_path_sort(
            media_items,
            group_path,
            mode=sort_mode,
            path_key=lambda item: item["path"],
        )
        if media_items:
            groups.append({
                "group_name": group_path.name,
                "group_path": group_path,
                "items": media_items,
            })
    return groups, ignored, errors, warnings, size_skips


@functools.lru_cache(maxsize=128)
def _get_section_re(section: str):
    return re.compile(rf"(?ms)^(\[{re.escape(section)}\]\s*$)(.*?)(?=^\[|\Z)")


@functools.lru_cache(maxsize=128)
def _get_key_re(key: str):
    return re.compile(rf"(?m)^(\s*{re.escape(key)}\s*=\s*).*$")


def _update_toml_value(text: str, section: str, key: str, value) -> str:
    if isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, int):
        literal = str(value)
    elif isinstance(value, float):
        literal = repr(value)
    else:
        literal = json.dumps(str(value), ensure_ascii=False)

    section_re = _get_section_re(section)
    match = section_re.search(text)
    if not match:
        suffix = "\n" if text and not text.endswith("\n") else ""
        return f"{text}{suffix}\n[{section}]\n{key} = {literal}\n"

    body = match.group(2)
    key_re = _get_key_re(key)
    key_match = key_re.search(body)
    if key_match:
        body = body[: key_match.start()] + key_match.group(1) + literal + body[key_match.end() :]
    else:
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"{key} = {literal}\n"
    return text[: match.start(2)] + body + text[match.end(2) :]


def _write_config_values(values: dict[tuple[str, str], object]) -> str:
    try:
        if not CONFIG_PATH.exists():
            if not TEMPLATE_CONFIG_PATH.exists():
                return "找不到 config.toml 和 config.example.toml。"
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(TEMPLATE_CONFIG_PATH, CONFIG_PATH)
        text = CONFIG_PATH.read_text(encoding="utf-8")
        for (section, key), value in values.items():
            text = _update_toml_value(text, section, key, value)
        tomllib.loads(text)
        previous = CONFIG_PATH.read_text(encoding="utf-8")
        temp = CONFIG_PATH.with_suffix(".toml.tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, CONFIG_PATH)
        error = _reload_config()
        if error:
            temp.write_text(previous, encoding="utf-8")
            os.replace(temp, CONFIG_PATH)
            _reload_config()
        return error
    except Exception as exc:
        return f"配置保存失败：{type(exc).__name__}: {exc}"


def _load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        value = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except Exception:
        return []


def _save_history(records: list[dict]) -> None:
    try:
        HISTORY_PATH.write_text(
            json.dumps(records[-100:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


HISTORY_PATH = RUNTIME_HISTORY_PATH
CACHE_TARGETS = {
    "video_state": ("视频上传状态", VIDEO_STATE_DIR),
    "image_state": ("图片上传状态", IMAGE_STATE_DIR),
    "mixed_state": ("混合上传状态", MIXED_STATE_DIR),
    "thumb_cache": ("视频封面缓存", THUMBNAIL_CACHE_DIR),
    "image_compression": ("图片压缩缓存", IMAGE_COMPRESSION_CACHE_DIR),
    "video_album_captions": ("视频 Album 标题", CAPTIONS_DIR / "video.json"),
    "image_album_captions": ("图片 Album 标题", CAPTIONS_DIR / "image.json"),
    "mixed_album_captions": ("混合 Album 标题", CAPTIONS_DIR / "mixed.json"),
    "upload_inflight": ("未确认上传记录", UPLOAD_INFLIGHT_DIR),
    "staging": ("本地暂存文件", STAGING_CACHE_DIR),
    "gui_history": ("GUI 历史记录", HISTORY_PATH),
    "logs": ("运行日志", LOG_DIR),
}
ALL_CACHE_KEYS = tuple(CACHE_TARGETS)


def _current_cache_targets() -> dict:
    """Resolve dynamic cache locations, especially configured staging."""

    targets = dict(CACHE_TARGETS)
    targets["staging"] = ("本地暂存文件", Path(getattr(cfg, "STAGING_DIR", STAGING_CACHE_DIR)))
    return targets


def _cache_usage(path: Path) -> tuple[int, int]:
    """Return file count and byte size without following a directory symlink."""
    try:
        if is_link_or_junction(path):
            return (1, path.lstat().st_size)
        if path.is_file():
            return (1, path.stat().st_size)
        if not path.is_dir():
            return (0, 0)
        count = 0
        total = 0

        stack = [os.fspath(path)]
        while stack:
            current_dir = stack.pop()
            try:
                with os.scandir(current_dir) as it:
                    for entry in it:
                        try:
                            # Managed staging markers are bookkeeping, not
                            # user cache artifacts; keep the status count
                            # focused on files that can actually be uploaded.
                            if entry.name == ".marker.json":
                                continue
                            is_junction = getattr(entry, "is_junction", None)
                            if entry.is_symlink() or (
                                callable(is_junction) and is_junction()
                            ):
                                continue
                            info = entry.stat(follow_symlinks=False)
                            if stat.S_ISREG(info.st_mode):
                                count += 1
                                total += info.st_size
                            elif stat.S_ISDIR(info.st_mode):
                                stack.append(entry.path)
                        except OSError:
                            continue
            except OSError:
                continue

        return (count, total)
    except OSError:
        return (0, 0)


def _cache_status_text() -> str:
    rows = []
    for label, path in _current_cache_targets().values():
        if not (path.exists() or path.is_symlink()):
            continue
        count, total = _cache_usage(path)
        rows.append(f"{label} {count} 项 · {_fmt_size(total)}")
    return "当前应用缓存：" + (" · ".join(rows) if rows else "无")


def _remove_cache_path(path: Path) -> None:
    """Clear a known cache path while keeping its directory structure."""
    path = Path(path)
    if is_link_or_junction(path):
        # A Windows junction is a directory reparse point, so rmdir removes
        # the link itself without traversing its target. POSIX symlinks use
        # unlink; neither operation touches the external directory.
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink()
    elif path.is_file():
        path.unlink()
    elif path.is_dir():
        try:
            entries = list(os.scandir(path))
        except OSError:
            raise
        for entry in entries:
            child = Path(entry.path)
            try:
                junction = getattr(entry, "is_junction", None)
                if entry.is_symlink() or (callable(junction) and junction()):
                    # Remove the link itself, never its target.
                    if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                        child.rmdir()
                    else:
                        child.unlink(missing_ok=True)
                elif entry.is_file(follow_symlinks=False):
                    child.unlink(missing_ok=True)
                elif entry.is_dir(follow_symlinks=False):
                    _remove_cache_path(child)
                    child.rmdir()
            except OSError:
                raise


def _clear_cache(keys: tuple[str, ...]) -> tuple[list[str], list[str]]:
    removed = []
    errors = []
    for key in keys:
        label, path = _current_cache_targets()[key]
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            _remove_cache_path(path)
            removed.append(label)
        except OSError as exc:
            errors.append(f"{label}：{exc}")
    return removed, errors


def _cancelled_scan_result(
    kind: str,
    *,
    core_available: bool,
    warning: str = "扫描已取消",
    ignored_root_media=None,
    scan_errors=None,
    scan_warnings=None,
    scan_size_skips=None,
) -> dict:
    """Build an explicit cancellation result instead of a fake empty scan."""

    kind = _require_kind(kind)
    return {
        "kind": kind,
        "status": "cancelled",
        "cancelled": True,
        "items": [],
        "missing": [],
        "groups": [],
        "total_files": 0,
        "completed_files": 0,
        "pending_files": 0,
        "total_bytes": 0,
        "pending_bytes": 0,
        "album_count": 0,
        "completed_paths": [],
        "source_dir": str(_cfg(KIND_PATH_KEYS[kind], "")),
        "state_path": "",
        "core_available": core_available,
        "warning": warning,
        "scan_errors": list(scan_errors or []),
        "scan_warnings": list(scan_warnings or []),
        "scan_size_skips": list(scan_size_skips or []),
        "scan_skipped_files": 0,
        "scan_compress_files": 0,
        "ignored_root_media": [str(path) for path in (ignored_root_media or [])],
        "target": _target_for(kind),
    }


def _scan_result(kind: str, progress_callback=None, cancel_event=None) -> dict:
    """Scan using the existing core when available, with a preview fallback."""
    kind = str(kind).strip().lower()
    if kind not in MEDIA_KINDS:
        raise ValueError(f"未知媒体类型：{kind}")
    _path_size.cache_clear()
    if cfg is None:
        raise RuntimeError(_CONFIG_ERROR or "配置不可用。")
    activate = getattr(cfg, "activate_target", None)
    if callable(activate):
        activate(kind)

    core = None
    mixed_groups = []
    ignored_root_media: list[Path] = []
    warning = ""
    scan_errors: list[str] = []
    scan_warnings: list[str] = []
    scan_size_skips: list[dict] = []
    try:
        if kind == "video":
            import tdlib_video_album_uploader as core_module
        elif kind == "image":
            import tdlib_image_album_uploader as core_module
        elif kind == "mixed":
            import tdlib_mixed_album_uploader as core_module
        else:
            raise ValueError(f"未知媒体类型：{kind}")
        core = core_module
    except Exception as exc:
        warning = f"当前环境尚未加载完整上传依赖，预览使用基础扫描：{exc}"

    state = None
    if kind == "video":
        if core is not None:
            core.STATE_DIR = VIDEO_STATE_DIR
            paths = (
                core.scan_videos()
                if cancel_event is None
                else core.scan_videos(cancel_event=cancel_event)
            )
            scan_errors = list(getattr(core, "LAST_SCAN_ERRORS", []))
            scan_warnings = list(getattr(core, "LAST_SCAN_WARNINGS", []))
            scan_size_skips = list(getattr(core, "LAST_SCAN_SIZE_SKIPS", []))
            metadata = {}
            exiftool = Path(_cfg("EXIFTOOL_PATH", ""))
            read_dates = bool(_cfg("VIDEO_READ_DATES", True))
            if progress_callback is not None:
                progress_callback({
                    "phase": "exif" if read_dates else "date_disabled",
                    "completed": 0,
                    "total": len(paths),
                })
            if not read_dates:
                warning = "已关闭日期读取，将按文件名扫描并按固定数量分组。"
            elif exiftool.exists():
                try:
                    metadata = core.read_exif_metadata(
                        paths,
                        cancel_event=cancel_event,
                        progress_callback=progress_callback,
                    )
                except Exception as exc:
                    # A transient network share/tool failure should not make a
                    # complete preview disappear. build_items will still use
                    # media-date/mtime fallback according to configuration.
                    warning = f"ExifTool 读取失败，将使用后备日期：{exc}"
                    scan_errors.append(str(exc))
            elif _cfg("VIDEO_READ_MEDIA_CREATION_DATE", True):
                warning = (
                    "未找到 ExifTool，EXIF 日期不可用；"
                    "缺少 EXIF 的视频仍会尝试读取媒体创建日期，失败后使用文件修改时间。"
                )
            elif _cfg("VIDEO_MISSING_DATE_POLICY", "mtime") == "mtime":
                warning = "未找到 ExifTool，缺失 EXIF 的视频将使用文件修改时间。"
            else:
                warning = (
                    "未找到 ExifTool，无法读取 EXIF；"
                    "当前未启用媒体日期回退，缺失日期的视频会被标记。"
                )
            if cancel_event is not None and cancel_event.is_set():
                return _cancelled_scan_result(
                    kind,
                    core_available=core is not None,
                    warning="扫描已取消",
                )
            if cancel_event is None:
                items, missing = core.build_items(
                    paths,
                    metadata,
                    progress_callback=progress_callback,
                )
            else:
                items, missing = core.build_items(
                    paths,
                    metadata,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                )
            state = core.UploadState()
        else:
            paths, scan_size_skips = _apply_size_limits(
                _basic_paths(kind, cancel_event=cancel_event), kind
            )
            missing = []
            items = []
            if not _cfg("VIDEO_READ_DATES", True):
                items = [
                    {
                        "path": path,
                        "capture_time": None,
                        "month_key": "__all_videos__",
                        "date_tag": "未读取日期",
                        "fallback": False,
                        **(
                            {
                                "scan_size": BASIC_SCAN_SNAPSHOTS[stable_path(path)][0],
                                "scan_mtime_ns": BASIC_SCAN_SNAPSHOTS[stable_path(path)][1],
                            }
                            if stable_path(path) in BASIC_SCAN_SNAPSHOTS
                            else {}
                        ),
                    }
                    for path in paths
                ]
                warning = "已关闭日期读取，将按文件名扫描并按固定数量分组。"
            else:
                for path in paths:
                    capture_time = _dt.datetime.fromtimestamp(file_mtime(path))
                    items.append(
                        {
                            "path": path,
                            "capture_time": capture_time,
                            "month_key": _fmt_date(
                                capture_time,
                                "%Y-%m",
                                "__all_videos__",
                            ),
                            "date_tag": "FileSystem:ModifyTime",
                            "fallback": True,
                            **(
                                {
                                    "scan_size": BASIC_SCAN_SNAPSHOTS[stable_path(path)][0],
                                    "scan_mtime_ns": BASIC_SCAN_SNAPSHOTS[stable_path(path)][1],
                                }
                                if stable_path(path) in BASIC_SCAN_SNAPSHOTS
                                else {}
                            ),
                        }
                    )
    elif kind == "mixed":
        if core is not None:
            core.STATE_DIR = MIXED_STATE_DIR
            mixed_groups = (
                core.scan_mixed_groups()
                if cancel_event is None
                else core.scan_mixed_groups(cancel_event=cancel_event)
            )
            items = core.flatten_items(mixed_groups)
            scan_errors = list(getattr(core, "LAST_SCAN_ERRORS", []))
            scan_warnings = list(getattr(core, "LAST_SCAN_WARNINGS", []))
            scan_size_skips = list(getattr(core, "LAST_SCAN_SIZE_SKIPS", []))
            ignored_root_media = list(getattr(core, "LAST_SCAN_IGNORED_ROOT_MEDIA", []))
            state = core.UploadState()
        else:
            root = Path(_cfg("MIXED_DIR", PROJECT_DIR))
            (
                mixed_groups,
                ignored_root_media,
                scan_errors,
                scan_warnings,
                scan_size_skips,
            ) = _basic_mixed_scan(
                root, cancel_event=cancel_event
            )
            items = [item for group in mixed_groups for item in group["items"]]
        missing = []
    else:
        if core is not None:
            paths = (
                core.scan_images()
                if cancel_event is None
                else core.scan_images(cancel_event=cancel_event)
            )
            scan_errors = list(getattr(core, "LAST_SCAN_ERRORS", []))
            scan_warnings = list(getattr(core, "LAST_SCAN_WARNINGS", []))
            scan_size_skips = list(getattr(core, "LAST_SCAN_SIZE_SKIPS", []))
            state = core.UploadState()
        else:
            paths, scan_size_skips = _apply_size_limits(
                _basic_paths(kind, cancel_event=cancel_event), kind
            )
        items = paths
        missing = []

    if cancel_event is not None and cancel_event.is_set():
        return _cancelled_scan_result(
            kind,
            core_available=core is not None,
            warning="扫描已取消",
            ignored_root_media=ignored_root_media,
            scan_errors=scan_errors,
            scan_warnings=scan_warnings,
            scan_size_skips=scan_size_skips,
        )

    completion_cache = {}
    def completed(item) -> bool:
        path = item["path"] if isinstance(item, dict) else item
        key = str(path)
        if key not in completion_cache:
            completion_cache[key] = bool(state is not None and state.is_completed(item))
        return completion_cache[key]

    caption_store = CaptionStore(kind)

    groups = []
    if kind == "video":
        force_ten = (
            bool(core.force_ten_per_album())
            if core is not None
            else str(
                _cfg(
                    "VIDEO_GROUP_MODE",
                    "fixed" if _cfg("VIDEO_FORCE_TEN_PER_ALBUM", False) else "date",
                )
            ).lower() == "fixed"
        )
        if not _cfg("VIDEO_READ_DATES", True):
            force_ten = True
        include_group_title = bool(_cfg("VIDEO_CAPTION_INCLUDE_GROUP_TITLE", True))
        forced_key = getattr(core, "FORCED_GROUP_KEY", "__all_videos__")
        if force_ten:
            grouped = {forced_key: list(items)}
        else:
            grouped = defaultdict(list)
            for item in items:
                grouped[item["month_key"]].append(item)
        album_size = int(_cfg("VIDEO_ALBUM_SIZE", 10))
        core_plans = defaultdict(list)
        if core is not None:
            for plan in core.build_album_plans(items, state):
                core_plans[plan["month_key"]].append(plan)
        for month in sorted(grouped):
            month_items = grouped[month]
            if core is not None:
                plans = core_plans[month]
            else:
                plans = []
                for offset in range(0, len(month_items), album_size):
                    album_items = list(month_items[offset:offset + album_size])
                    default_caption = ""
                    if include_group_title:
                        default_caption = (
                            f"Album {offset // album_size + 1}"
                            if force_ten
                            else month[2:].lstrip("0")
                        )
                    key_group = f"{month}:{offset // album_size + 1}" if force_ten else month
                    key = album_key(
                        "video",
                        key_group,
                        album_items,
                        root=Path(_cfg("VIDEO_DIR", PROJECT_DIR)),
                        snapshot_provider=lambda path: BASIC_SCAN_SNAPSHOTS.get(stable_path(path)),
                    )
                    record = caption_store.get(key, default_caption)
                    base_label = record["base_label"] if include_group_title else ""
                    plans.append({
                        "key": key,
                        "month_key": month,
                        "number": offset // album_size + 1,
                        "items": album_items,
                        "pending_items": [item for item in album_items if not completed(item)],
                        "caption": {
                            "base_label": base_label,
                            "custom_text": record["custom_text"],
                            "text": compose_caption(base_label, record["custom_text"], " · "),
                        },
                    })
            pending = [item for item in month_items if not completed(item)]
            pending_plans = [plan for plan in plans if plan["pending_items"]]
            group_label = (
                getattr(core, "group_display_name", lambda value: value)(month)
                if core is not None
                else ("全部视频（按顺序分组）" if force_ten else month)
            )
            groups.append(
                {
                    "label": group_label,
                    "caption": plans[0]["caption"]["text"] if plans else "",
                    "items": month_items,
                    "pending": len(pending),
                    "completed": len(month_items) - len(pending),
                    "albums": len(pending_plans),
                    "album_plans": plans,
                }
            )
    elif kind == "mixed":
        if core is not None:
            plans = core.build_album_plans(mixed_groups, state)
        else:
            plans = []
            album_size = int(_cfg("MIXED_ALBUM_SIZE", 10))
            include_title = bool(_cfg("MIXED_CAPTION_INCLUDE_GROUP_TITLE", True))
            for mixed_group in mixed_groups:
                group_name = str(mixed_group["group_name"])
                for offset in range(0, len(mixed_group["items"]), album_size):
                    album_items = list(mixed_group["items"][offset:offset + album_size])
                    number = offset // album_size + 1
                    key = album_key(
                        "mixed",
                        f"{group_name}:{number}",
                        album_items,
                        root=root,
                        snapshot_provider=lambda path: BASIC_SCAN_SNAPSHOTS.get(stable_path(path)),
                    )
                    record = caption_store.get(key, group_name)
                    base_label = record["base_label"] if include_title else ""
                    plans.append({
                        "key": key,
                        "group_name": group_name,
                        "group_path": mixed_group.get("group_path"),
                        "number": number,
                        "items": album_items,
                        "pending_items": [item for item in album_items if not completed(item)],
                        "caption": {
                            "base_label": base_label,
                            "custom_text": record["custom_text"],
                            "text": compose_caption(base_label, record["custom_text"], " · "),
                        },
                    })
        for mixed_group in mixed_groups:
            group_name = str(mixed_group["group_name"])
            group_path = mixed_group.get("group_path")
            group_items = list(mixed_group.get("items", []))
            group_plans = [
                plan for plan in plans
                if (
                    plan.get("group_path") == group_path
                    if group_path is not None
                    else plan.get("group_name") == group_name
                )
            ]
            pending = [item for item in group_items if not completed(item)]
            groups.append({
                "label": group_name,
                "caption": group_plans[0]["caption"]["text"] if group_plans else "",
                "items": group_items,
                "pending": len(pending),
                "completed": len(group_items) - len(pending),
                "albums": sum(bool(plan.get("pending_items")) for plan in group_plans),
                "album_plans": group_plans,
            })
    elif kind == "image":
        album_size = int(_cfg("IMAGE_ALBUM_SIZE", 10))
        if core is not None:
            plans = core.build_album_plans(items, state)
        else:
            plans = []
            for offset in range(0, len(items), album_size):
                album_items = list(items[offset:offset + album_size])
                number = offset // album_size + int(_cfg("IMAGE_ALBUM_NUMBER_START", 1))
                key = album_key(
                    "image",
                    f"Album {number}",
                    album_items,
                    root=Path(_cfg("IMAGE_DIR", PROJECT_DIR)),
                    snapshot_provider=lambda path: BASIC_SCAN_SNAPSHOTS.get(stable_path(path)),
                )
                record = caption_store.get(key, str(number))
                plans.append({
                    "key": key,
                    "number": number,
                    "items": album_items,
                    "pending_items": [item for item in album_items if not completed(item)],
                    "caption": {
                        "base_label": record["base_label"],
                        "custom_text": record["custom_text"],
                        "text": compose_caption(record["base_label"], record["custom_text"], " · "),
                    },
                })
        for plan in plans:
            album_items = plan["items"]
            pending = plan["pending_items"]
            groups.append(
                {
                    "label": f"Album {plan['number']}",
                    "caption": plan["caption"]["text"],
                    "items": album_items,
                    "pending": len(pending),
                    "completed": len(album_items) - len(pending),
                    "albums": 1 if pending else 0,
                    "album_plans": [plan],
                }
            )

    completed_count = sum(1 for item in items if completed(item))
    total_bytes = sum(_item_size(item) for item in items)
    if scan_errors:
        write_app_log(
            "WARNING",
            "目录扫描跳过项目：\n" + "\n".join(scan_errors),
            source=f"scan/{kind}",
        )
        warning = (
            f"扫描时跳过 {len(scan_errors)} 个暂时无法读取的项目（可能是 SMB 连接中断）。"
            + (f"；{warning}" if warning else "")
        )
    if scan_warnings:
        write_app_log(
            "WARNING",
            "目录扫描提醒：\n" + "\n".join(scan_warnings),
            source=f"scan/{kind}",
        )
        warning_text = "；".join(str(value) for value in scan_warnings if value)
        if warning_text:
            warning = warning_text + (f"；{warning}" if warning else "")

    scan_rejected = [
        record for record in scan_size_skips
        if record.get("action", "skip") == "skip"
    ]
    scan_compressing = [
        record for record in scan_size_skips
        if record.get("action") == "compress"
    ]
    if scan_size_skips:
        write_app_log(
            "WARNING",
            "目录扫描大小限制处理：\n" + "\n".join(
                f"{record['path']} · {record.get('reason', '')} · "
                f"处理={'上传时压缩' if record.get('action') == 'compress' else '扫描时跳过'}"
                for record in scan_size_skips
            ),
            source=f"scan/{kind}",
        )
        notices = []
        if scan_rejected:
            limit_label = {
                "video": "视频约 4 GB",
                "image": "Photo 10 MiB",
                "mixed": "图片/视频",
            }.get(kind, _kind_label(kind))
            notices.append(
                f"扫描时跳过 {len(scan_rejected)} 个超过 "
                f"Telegram {limit_label} 上限的项目"
            )
        if scan_compressing:
            notices.append(
                f"发现 {len(scan_compressing)} 个超限图片，将在上传时使用 FFmpeg 压缩临时副本"
            )
        size_warning = "；".join(notices)
        warning = size_warning + (f"；{warning}" if warning else "")

    if ignored_root_media:
        write_app_log(
            "WARNING",
            "混合根目录中的媒体已忽略（请移动到一级子文件夹）：\n"
            + "\n".join(str(path) for path in ignored_root_media),
            source=f"scan/{kind}",
        )
        ignored_warning = (
            f"混合根目录中有 {len(ignored_root_media)} 个媒体已忽略，"
            "请移动到一级子文件夹后重新扫描"
        )
        warning = ignored_warning + (f"；{warning}" if warning else "")

    return {
        "kind": kind,
        "items": items,
        "missing": missing,
        "groups": groups,
        "total_files": len(items),
        "completed_files": completed_count,
        "pending_files": len(items) - completed_count,
        "total_bytes": total_bytes,
        "pending_bytes": sum(_item_size(item) for item in items if not completed(item)),
        "album_count": sum(group["albums"] for group in groups),
        "completed_paths": [
            stable_path(item["path"] if isinstance(item, dict) else item)
            for item in items
            if completed(item)
        ],
        "source_dir": str(_cfg(KIND_PATH_KEYS[kind], "")),
        "state_path": str(state.path) if state is not None else "",
        "core_available": core is not None,
        "warning": warning,
        "scan_warnings": scan_warnings,
        "cancelled": bool(cancel_event is not None and cancel_event.is_set()),
        "scan_size_skips": scan_size_skips,
        "scan_skipped_files": len(scan_rejected),
        "scan_compress_files": len(scan_compressing),
        "ignored_root_media": [str(path) for path in ignored_root_media],
        "target": _target_for(kind),
    }


class ScanWorker(QThread):
    completed = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    progress_changed = Signal(str, object)

    def __init__(self, kind: str):
        super().__init__()
        self.kind = _require_kind(kind)
        self.cancel_event = threading.Event()

    def request_stop(self):
        """Request cancellation without terminating the worker thread."""

        self.cancel_event.set()

    def _report_progress(self, payload: dict):
        self.progress_changed.emit(self.kind, payload)

    def run(self):
        try:
            result = _scan_result(
                self.kind,
                progress_callback=self._report_progress,
                cancel_event=self.cancel_event,
            )
            if result.get("cancelled"):
                self.cancelled.emit(result)
            else:
                self.completed.emit(result)
        except Exception as exc:
            write_exception(f"{self.kind} 扫描失败", exc, source=f"scan/{self.kind}")
            self.failed.emit(f"扫描失败：{type(exc).__name__}: {exc}")


class AuthBridge(QObject):
    requested = Signal(str, bool)

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._event: threading.Event | None = None
        self._value = ""

    def ask(self, prompt: str, password: bool = False) -> str:
        event = threading.Event()
        with self._lock:
            self._event = event
            self._value = ""
        self.requested.emit(prompt, password)
        event.wait(3600)
        with self._lock:
            value = self._value
            self._event = None
        return value

    def answer(self, value: str):
        with self._lock:
            self._value = value
            event = self._event
        if event is not None:
            event.set()


class GuiConsoleUI(QObject):
    """GUI signal adapter implementing the backend upload callbacks."""

    message_added = Signal(str, str)
    progress_changed = Signal(object)
    album_changed = Signal(object)
    target_changed = Signal(object)

    def __init__(self, auth_bridge: AuthBridge, kind: str = ""):
        super().__init__()
        self.auth_bridge = auth_bridge
        self.kind = kind
        self._client = None
        self._client_lock = threading.Lock()
        self._stop_requested = threading.Event()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    @property
    def cancel_event(self):
        """Expose the shared event to scan/media helpers."""

        return self._stop_requested

    def register_client(self, client):
        with self._client_lock:
            self._client = client
            stop_already_requested = self._stop_requested.is_set()
        if stop_already_requested:
            # The user can press stop while TDLib is still starting.  Carry
            # that request into the newly created client instead of allowing
            # the first upload to begin.
            client.cancel()

    def request_stop(self):
        self._stop_requested.set()
        self.auth_bridge.answer("")
        with self._client_lock:
            client = self._client
        if client is not None:
            client.cancel()

    def prompt(self, text: str, *, password: bool = False) -> str:
        value = self.auth_bridge.ask(text, password)
        if not value:
            self.request_stop()
        return value

    def _message(self, level: str, text):
        message = str(text)
        level_name = {
            "log": "INFO",
            "info": "INFO",
            "success": "INFO",
            "banner": "INFO",
            "summary": "INFO",
            "warning": "WARNING",
            "error": "ERROR",
        }.get(level, "INFO")
        write_app_log(level_name, message, source=f"gui/{self.kind or 'app'}")
        self.message_added.emit(level, message)

    def log(self, text=""):
        self._message("log", text)

    def info(self, text):
        self._message("info", text)

    def success(self, text):
        self._message("success", text)

    def warning(self, text):
        self._message("warning", text)

    def error(self, text):
        self._message("error", text)

    def banner(self, title: str, subtitle: str = "", *, accent="cyan"):
        self._message("banner", f"{title}\n{subtitle}".strip())

    def summary(self, title, rows, *, kind="VIDEO"):
        body = [str(title)] + [f"{key}: {value}" for key, value in rows]
        self._message("summary", "\n".join(body))

    def files(self, title, columns, rows, *, kind="VIDEO", caption=None):
        suffix = f"\n{caption}" if caption else ""
        self._message("info", f"{title} · {len(list(rows))} 项{suffix}")

    def groups(self, title, rows, *, kind="VIDEO"):
        self._message("info", f"{title} · {len(list(rows))} 组")

    def target(self, chat_title, topic_name, chat_id, topic_id):
        payload = {
            "kind": self.kind,
            "target_mode": str(_cfg("TARGET_MODE", "forum_topic")),
            "chat_title": chat_title or "(未命名)",
            "topic_name": topic_name or "",
            "chat_id": chat_id,
            "topic_id": topic_id,
        }
        self.target_changed.emit(payload)
        suffix = f" / {payload['topic_name']}" if payload["topic_name"] else "（频道）"
        self._message("success", f"Telegram 目标：{payload['chat_title']}{suffix}")

    def album(self, *, kind, title, subtitle="", rows=None):
        self.album_changed.emit({
            "kind": kind,
            "title": title,
            "subtitle": subtitle,
            "rows": list(rows or []),
        })

    def confirm_upload(self) -> bool:
        return not self.stop_requested

    def cancelled(self):
        self.warning("已取消，没有开始上传。")

    def progress(self, **kwargs):
        self.progress_changed.emit(dict(kwargs))

    def finish(self):
        return None


class UploadWorker(QThread):
    completed = Signal(bool, str)

    def __init__(self, kind: str, auth_bridge: AuthBridge):
        super().__init__()
        self.kind = _require_kind(kind)
        self.ui = GuiConsoleUI(auth_bridge, kind)

    def request_stop(self):
        self.ui.request_stop()

    def run(self):
        try:
            activate = getattr(cfg, "activate_target", None)
            if callable(activate):
                activate(self.kind)
            if self.kind == "video":
                import tdlib_video_album_uploader as core
                import tdlib_video_app as entry

                core.STATE_DIR = VIDEO_STATE_DIR
                core.UI = self.ui
                core._INSTANCE_LOCK_HELD = True
                entry.UI = self.ui
                entry._INSTANCE_LOCK_HELD = True
                entry.main()
            elif self.kind == "image":
                import tdlib_image_album_uploader as core

                core.UI = self.ui
                core._INSTANCE_LOCK_HELD = True
                core.main()
            elif self.kind == "mixed":
                import tdlib_mixed_album_uploader as core

                core.STATE_DIR = MIXED_STATE_DIR
                core.UI = self.ui
                core._INSTANCE_LOCK_HELD = True
                core.main()
            else:
                raise ValueError(f"未知媒体类型：{self.kind}")
            if self.ui.stop_requested:
                self.completed.emit(False, "任务已立即停止；完整完成的 Album 已保存断点。")
            else:
                self.completed.emit(True, "上传任务完成。")
        except Exception as exc:
            if type(exc).__name__ == "TDLibCancelled" or self.ui.stop_requested:
                self.completed.emit(False, "任务已立即停止；完整完成的 Album 已保存断点。")
            else:
                self.ui.error(f"程序停止：{type(exc).__name__}: {exc}")
                write_exception(
                    f"{self.kind} 上传线程失败",
                    exc,
                    source=f"upload/{self.kind}",
                )
                self.completed.emit(False, f"任务失败：{type(exc).__name__}: {exc}")


def _card(title: str, value: str = "—") -> tuple[QFrame, QLabel]:
    frame = QFrame()
    frame.setObjectName("statCard")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 14, 18, 14)
    title_label = QLabel(title)
    title_label.setObjectName("mutedLabel")
    value_label = QLabel(value)
    value_label.setObjectName("statValue")
    layout.addWidget(title_label)
    layout.addWidget(value_label)
    return frame, value_label


class HomePage(QWidget):
    start_upload = Signal(str)
    open_settings = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        heading = QLabel("概览")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("本地媒体 → Telegram 超级群组 Topic 或 Channel")
        subtitle.setObjectName("mutedLabel")
        layout.addWidget(heading)
        layout.addWidget(subtitle)

        stats = QGridLayout()
        stats.setSpacing(12)
        self.connection_card, self.connection_value = _card("Telegram 状态", "未连接")
        self.task_card, self.task_value = _card("当前任务", "无")
        self.today_card, self.today_value = _card("本次扫描", "—")
        stats.addWidget(self.connection_card, 0, 0)
        stats.addWidget(self.task_card, 0, 1)
        stats.addWidget(self.today_card, 0, 2)
        layout.addLayout(stats)

        task_box = QGroupBox("快速开始")
        task_layout = QHBoxLayout(task_box)
        task_layout.setContentsMargins(18, 20, 18, 20)
        video = QPushButton("上传视频")
        video.setObjectName("primaryButton")
        image = QPushButton("上传图片")
        image.setObjectName("secondaryButton")
        mixed = QPushButton("混合上传")
        mixed.setObjectName("secondaryButton")
        settings = QPushButton("配置与诊断")
        settings.setObjectName("secondaryButton")
        video.clicked.connect(lambda: self.start_upload.emit("video"))
        image.clicked.connect(lambda: self.start_upload.emit("image"))
        mixed.clicked.connect(lambda: self.start_upload.emit("mixed"))
        settings.clicked.connect(self.open_settings)
        task_layout.addWidget(video)
        task_layout.addWidget(image)
        task_layout.addWidget(mixed)
        task_layout.addWidget(settings)
        task_layout.addStretch(1)
        layout.addWidget(task_box)

        note = QGroupBox(f"V{APP_VERSION} 运行提示")
        note_layout = QVBoxLayout(note)
        note_body = QLabel(
            "先配置 Telegram 信息，再选择目录并扫描。视频、图片和混合上传可分别设置目标。\n"
            "选中媒体组即可编辑标题；确认预览后开始上传。\n"
            "一次只能运行一个任务。安全停止后重新扫描，已完成的媒体组会自动跳过。"
        )
        note_body.setWordWrap(True)
        note_layout.addWidget(note_body)
        layout.addWidget(note)
        layout.addStretch(1)
        self.set_connection("未连接", False)

    def update_scan(self, result: dict):
        self.today_value.setText(
            f"{result['total_files']} 个文件 · {_fmt_size(result['total_bytes'])}"
        )

    def clear_scan(self):
        self.today_value.setText("—")

    def set_connection(self, text: str, good: bool = False):
        self.connection_value.setText(text)
        self.connection_value.setProperty("good", good)
        self.connection_value.style().unpolish(self.connection_value)
        self.connection_value.style().polish(self.connection_value)


class UploadPage(QWidget):
    start_requested = Signa…12925 tokens truncated…分，每组 1~10 个媒体。")
            mixed_form.addRow("每组媒体数", self.mixed_album)
            self.mixed_group_title = QCheckBox("带文件夹组标题")
            self.mixed_group_title.setChecked(bool(_cfg("MIXED_CAPTION_INCLUDE_GROUP_TITLE", True)))
            self.mixed_group_title.setToolTip("默认使用一级子文件夹名称作为 Album 标题。")
            mixed_form.addRow("组标题", self.mixed_group_title)
            self.mixed_filenames = QCheckBox("带文件名（仅标题，不含扩展名）")
            self.mixed_filenames.setChecked(bool(_cfg("MIXED_CAPTION_INCLUDE_FILENAMES", False)))
            mixed_form.addRow("文件名列表", self.mixed_filenames)
            self.mixed_filename_numbers = QCheckBox("文件名带序号（1、2、3…）")
            self.mixed_filename_numbers.setChecked(bool(_cfg("MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS", True)))
            self.mixed_filename_numbers.setToolTip(
                "关闭后只显示文件名，每行一个，不添加序号；"
                "即使暂时关闭文件名列表，也可以先保存这个格式选项。"
            )
            mixed_form.addRow("文件名格式", self.mixed_filename_numbers)
            self.mixed_separator = QLineEdit(str(_cfg("MIXED_ALBUM_CAPTION_SEPARATOR", " · ")))
            mixed_form.addRow("标题分隔符", self.mixed_separator)
            self.mixed_thumbnail = QCheckBox("视频生成缩略图")
            self.mixed_thumbnail.setChecked(bool(_cfg("MIXED_GENERATE_THUMBNAIL", True)))
            mixed_form.addRow("视频处理", self.mixed_thumbnail)
            self.media_layout.addWidget(mixed_box)

    def _load_target(self):
        target = _target_for(self.kind)
        self.target_mode.blockSignals(True)
        index = self.target_mode.findData(target.get("target_mode", "forum_topic"))
        self.target_mode.setCurrentIndex(index if index >= 0 else 0)
        self.target_mode.blockSignals(False)
        self.chat_id.setText(str(target.get("group_chat_id", 0) or ""))
        self.channel_chat_id.setText(str(target.get("channel_chat_id", 0) or ""))
        self.topic_id.setText(str(target.get("forum_topic_id", 0) or ""))
        self._update_fields()

    def _update_fields(self):
        channel = self.target_mode.currentData() == "channel"
        for field, visible in (
            (self.chat_id, not channel),
            (self.channel_chat_id, channel),
            (self.topic_id, not channel),
        ):
            field.setVisible(visible)
            label = self.form.labelForField(field)
            if label is not None:
                label.setVisible(visible)

    def _update_video_date_fields(self):
        if self.kind != "video" or not hasattr(self, "video_read_dates"):
            return
        enabled = self.video_read_dates.isChecked()
        self.video_missing_date.setEnabled(enabled)
        self.video_media_creation.setEnabled(enabled)
        self.video_sort.setEnabled(enabled)
        self.video_group_mode.setEnabled(enabled)
        if not enabled:
            name_index = self.video_sort.findData("name")
            fixed_index = self.video_group_mode.findData("fixed")
            if name_index >= 0:
                self.video_sort.setCurrentIndex(name_index)
            if fixed_index >= 0:
                self.video_group_mode.setCurrentIndex(fixed_index)

    def _save(self):
        try:
            group_id = int(self.chat_id.text().strip() or "0")
            channel_id = int(self.channel_chat_id.text().strip() or "0")
            topic_id = int(self.topic_id.text().strip() or "0")
        except ValueError:
            QMessageBox.critical(self, "保存失败", "Chat ID 和 Topic ID 都必须是整数。")
            return
        mode = self.target_mode.currentData() or "forum_topic"
        if mode == "channel":
            if channel_id == 0:
                QMessageBox.critical(self, "保存失败", "频道 Chat ID 不能为 0。")
                return
        else:
            if group_id == 0:
                QMessageBox.critical(self, "保存失败", "群组 Chat ID 不能为 0。")
                return
            if topic_id <= 0:
                QMessageBox.critical(self, "保存失败", "Forum Topic ID 必须大于 0。")
                return
        values = {
            (f"telegram.{self.kind}", "target_mode"): mode,
            (f"telegram.{self.kind}", "chat_id"): group_id,
            (f"telegram.{self.kind}", "channel_chat_id"): channel_id,
            (f"telegram.{self.kind}", "forum_topic_id"): topic_id,
        }
        if self.kind == "video":
            read_dates = self.video_read_dates.isChecked()
            values.update({
                ("video", "missing_date_policy"): self.video_missing_date.currentData(),
                ("video", "read_media_creation_date"): self.video_media_creation.isChecked(),
                ("video", "read_dates"): read_dates,
                ("video", "sort_mode"): self.video_sort.currentData() if read_dates else "name",
                ("video", "album_size"): self.video_album.value(),
                ("video", "group_mode"): self.video_group_mode.currentData() if read_dates else "fixed",
                ("video", "force_ten_per_album"): (
                    self.video_group_mode.currentData() == "fixed"
                    if read_dates
                    else True
                ),
                ("video", "caption_include_group_title"): self.video_group_title.isChecked(),
                ("video", "album_caption_separator"): self.video_separator.text(),
                ("video", "caption_include_filenames"): self.video_filenames.isChecked(),
                ("video", "caption_include_filename_numbers"): self.video_filename_numbers.isChecked(),
                ("video", "generate_thumbnail"): self.thumbnail.isChecked(),
            })
        elif self.kind == "image":
            values.update({
                ("image", "sort_mode"): self.image_sort.currentData(),
                ("image", "album_size"): self.image_album.value(),
                ("image", "album_numbering"): self.image_numbering.isChecked(),
                ("image", "album_caption_separator"): self.image_separator.text(),
                ("image", "caption_include_filenames"): self.image_filenames.isChecked(),
                ("image", "compress_oversize"): self.image_compress.isChecked(),
            })
        else:
            values.update({
                ("mixed", "sort_mode"): self.mixed_sort.currentData(),
                ("mixed", "album_size"): self.mixed_album.value(),
                ("mixed", "caption_include_group_title"): self.mixed_group_title.isChecked(),
                ("mixed", "caption_include_filenames"): self.mixed_filenames.isChecked(),
                ("mixed", "caption_include_filename_numbers"): self.mixed_filename_numbers.isChecked(),
                ("mixed", "album_caption_separator"): self.mixed_separator.text(),
                ("mixed", "generate_thumbnail"): self.mixed_thumbnail.isChecked(),
            })
        error = _write_config_values(values)
        if error:
            QMessageBox.critical(self, "保存失败", error)
            return
        self.accept()


class ConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"编辑配置 · V{APP_VERSION}")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.fields = {}

        def field(key: str, value, password=False):
            widget = QLineEdit(str(value if value is not None else ""))
            if password:
                widget.setEchoMode(QLineEdit.EchoMode.Password)
            self.fields[key] = widget
            return widget

        form.addRow("API ID", field("api_id", _cfg("API_ID", 12345678)))
        form.addRow("API Hash", field("api_hash", _cfg("API_HASH", "YOUR_API_HASH"), True))
        form.addRow("视频目录", field("video_dir", _cfg("VIDEO_DIR", "")))
        form.addRow("图片目录", field("image_dir", _cfg("IMAGE_DIR", "")))
        form.addRow("混合目录", field("mixed_dir", _cfg("MIXED_DIR", "")))
        self.staging_enabled = QCheckBox("启用本地暂存（适合 SMB/NAS）")
        self.staging_enabled.setChecked(bool(_cfg("STAGING_ENABLED", False)))
        form.addRow("上传暂存", self.staging_enabled)
        self.staging_mode = QComboBox()
        self.staging_mode.addItem("关闭（直接读取源文件）", "off")
        self.staging_mode.addItem("仅网络盘", "network")
        self.staging_mode.addItem("所有文件", "always")
        configured_staging_mode = str(
            _cfg("STAGING_MODE", "always" if _cfg("STAGING_ENABLED", False) else "off")
        ).strip().lower()
        mode_index = self.staging_mode.findData(configured_staging_mode)
        self.staging_mode.setCurrentIndex(mode_index if mode_index >= 0 else 0)
        form.addRow("暂存模式", self.staging_mode)
        form.addRow("暂存目录", field("staging_dir", _cfg("STAGING_DIR", STAGING_CACHE_DIR)))
        self.staging_cleanup_on_start = QCheckBox("启动时清理过期暂存文件")
        self.staging_cleanup_on_start.setChecked(
            bool(_cfg("STAGING_CLEANUP_ON_START", True))
        )
        form.addRow("暂存清理", self.staging_cleanup_on_start)
        self.staging_cleanup_days = QSpinBox()
        self.staging_cleanup_days.setRange(0, 3650)
        self.staging_cleanup_days.setValue(int(_cfg("STAGING_CLEANUP_DAYS", 7)))
        self.staging_cleanup_days.setSuffix(" 天")
        form.addRow("暂存保留时间", self.staging_cleanup_days)
        self.staging_cleanup_after_success = QCheckBox("Album 确认成功后删除暂存副本")
        self.staging_cleanup_after_success.setChecked(
            bool(_cfg("STAGING_CLEANUP_AFTER_SUCCESS", True))
        )
        form.addRow("成功后清理", self.staging_cleanup_after_success)

        def sync_legacy_enabled(index):
            enabled = self.staging_mode.itemData(index) != "off"
            self.staging_enabled.blockSignals(True)
            self.staging_enabled.setChecked(enabled)
            self.staging_enabled.blockSignals(False)

        def sync_mode_from_legacy(enabled):
            if enabled and self.staging_mode.currentData() == "off":
                self.staging_mode.setCurrentIndex(self.staging_mode.findData("network"))
            elif not enabled:
                self.staging_mode.setCurrentIndex(self.staging_mode.findData("off"))

        self.staging_mode.currentIndexChanged.connect(sync_legacy_enabled)
        self.staging_enabled.toggled.connect(sync_mode_from_legacy)
        sync_legacy_enabled(self.staging_mode.currentIndex())
        content_layout.addLayout(form)


        proxy_box = QGroupBox("网络代理（独立设置，默认关闭）")
        proxy_form = QFormLayout(proxy_box)
        self.proxy_enabled = QCheckBox("启用代理（关闭时使用直连）")
        self.proxy_enabled.setChecked(bool(_cfg("PROXY_ENABLED", False)))
        proxy_form.addRow("代理状态", self.proxy_enabled)

        self.proxy_type = QComboBox()
        self.proxy_type.addItem("SOCKS5", "socks5")
        self.proxy_type.addItem("HTTP", "http")
        self.proxy_type.addItem("MTProto", "mtproto")
        configured_proxy_type = str(_cfg("PROXY_TYPE", "socks5")).lower()
        proxy_index = self.proxy_type.findData(configured_proxy_type)
        self.proxy_type.setCurrentIndex(proxy_index if proxy_index >= 0 else 0)
        proxy_type_label = QLabel("代理类型")
        proxy_form.addRow(proxy_type_label, self.proxy_type)

        self.proxy_server = field("proxy_server", _cfg("PROXY_SERVER", ""))
        proxy_server_label = QLabel("代理服务器")
        proxy_form.addRow(proxy_server_label, self.proxy_server)

        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(1, 65535)
        self.proxy_port.setValue(int(_cfg("PROXY_PORT", 1080)))
        proxy_port_label = QLabel("代理端口")
        proxy_form.addRow(proxy_port_label, self.proxy_port)

        self.proxy_username = field("proxy_username", _cfg("PROXY_USERNAME", ""))
        proxy_username_label = QLabel("代理用户名")
        proxy_form.addRow(proxy_username_label, self.proxy_username)

        self.proxy_password = field("proxy_password", _cfg("PROXY_PASSWORD", ""), True)
        proxy_password_label = QLabel("代理密码")
        proxy_form.addRow(proxy_password_label, self.proxy_password)

        self.proxy_secret = field("proxy_secret", _cfg("PROXY_SECRET", ""), True)
        proxy_secret_label = QLabel("MTProto Secret")
        proxy_form.addRow(proxy_secret_label, self.proxy_secret)

        self.proxy_http_only = QCheckBox("仅支持 HTTP 请求（不支持 CONNECT）")
        self.proxy_http_only.setChecked(bool(_cfg("PROXY_HTTP_ONLY", False)))
        proxy_http_only_label = QLabel("HTTP 选项")
        proxy_form.addRow(proxy_http_only_label, self.proxy_http_only)

        self._proxy_rows = {
            "username": (proxy_username_label, self.proxy_username),
            "password": (proxy_password_label, self.proxy_password),
            "secret": (proxy_secret_label, self.proxy_secret),
            "http_only": (proxy_http_only_label, self.proxy_http_only),
        }
        self.proxy_enabled.toggled.connect(self._update_proxy_fields)
        self.proxy_type.currentIndexChanged.connect(self._update_proxy_fields)
        self._update_proxy_fields()
        content_layout.addWidget(proxy_box)

        hint = QLabel(
            "视频、图片和混合上传的 Album、Caption 及处理选项请在各自上传页面的“编辑目标”中设置。"
            "API Hash、代理认证信息和 MTProto Secret 只写入本地 config.toml，不会写入 GUI 日志。"
            "代理由 TDLib 原生支持；tdjson 版本仍由项目固定要求控制。"
            "启用本地暂存后，发送前会把源文件复制到指定本地目录，断点仍以原始路径为准。"
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        content_layout.addWidget(hint)

        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(760, 700)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.setMaximumHeight(max(420, available.height() - 80))
            self.resize(
                min(self.width(), max(620, available.width() - 80)),
                min(self.height(), max(420, available.height() - 80)),
            )

    def _update_proxy_fields(self):
        enabled = self.proxy_enabled.isChecked()
        proxy_type = self.proxy_type.currentData()
        show_credentials = enabled and proxy_type in {"socks5", "http"}
        show_secret = enabled and proxy_type == "mtproto"
        show_http_only = enabled and proxy_type == "http"
        for key in ("username", "password"):
            label, widget = self._proxy_rows[key]
            label.setVisible(show_credentials)
            widget.setVisible(show_credentials)
        label, widget = self._proxy_rows["secret"]
        label.setVisible(show_secret)
        widget.setVisible(show_secret)
        label, widget = self._proxy_rows["http_only"]
        label.setVisible(show_http_only)
        widget.setVisible(show_http_only)
        for key in ("proxy_type", "proxy_server", "proxy_port"):
            getattr(self, key).setEnabled(enabled)
        for row in self._proxy_rows.values():
            for widget in row:
                widget.setEnabled(enabled)

    def _save(self):
        try:
            api_id = int(self.fields["api_id"].text().strip())
            if api_id <= 0:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "无法保存", "API ID 必须是正整数。")
            return

        values = {
            ("telegram", "api_id"): api_id,
            ("telegram", "api_hash"): self.fields["api_hash"].text().strip(),
            ("paths", "video_dir"): self.fields["video_dir"].text().strip(),
            ("paths", "image_dir"): self.fields["image_dir"].text().strip(),
            ("paths", "mixed_dir"): self.fields["mixed_dir"].text().strip(),
            ("staging", "enabled"): self.staging_mode.currentData() != "off",
            ("staging", "mode"): self.staging_mode.currentData() or "off",
            ("staging", "directory"): self.fields["staging_dir"].text().strip(),
            ("staging", "cleanup_on_start"): self.staging_cleanup_on_start.isChecked(),
            ("staging", "cleanup_days"): self.staging_cleanup_days.value(),
            ("staging", "cleanup_after_success"): self.staging_cleanup_after_success.isChecked(),
            ("proxy", "enabled"): self.proxy_enabled.isChecked(),
            ("proxy", "type"): self.proxy_type.currentData() or "socks5",
            ("proxy", "server"): self.proxy_server.text().strip(),
            ("proxy", "port"): self.proxy_port.value(),
            ("proxy", "username"): self.proxy_username.text(),
            ("proxy", "password"): self.proxy_password.text(),
            ("proxy", "secret"): self.proxy_secret.text().strip(),
            ("proxy", "http_only"): self.proxy_http_only.isChecked(),
        }
        if values[("proxy", "enabled")]:
            if not values[("proxy", "server")]:
                QMessageBox.critical(self, "保存失败", "启用代理时必须填写代理服务器。")
                return
            if values[("proxy", "type")] == "mtproto" and not values[("proxy", "secret")]:
                QMessageBox.critical(self, "保存失败", "使用 MTProto 代理时必须填写 Secret。")
                return
        error = _write_config_values(values)
        if error:
            QMessageBox.critical(self, "保存失败", error)
            return
        self.accept()


def _hidden_subprocess_kwargs() -> dict:
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


def _validate_exiftool_path(value: str) -> str:
    """Return a user-facing error when ExifTool cannot answer ``-ver`` quickly."""

    text = str(value or "").strip().strip('"')
    if not text:
        # An empty value keeps the existing automatic/default tool lookup.
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
        result = run_cancellable_process(
            [str(candidate), "-ver"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5.0,
            check=False,
            **_hidden_subprocess_kwargs(),
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


class ScanToolsDialog(QDialog):
    """Edit scan, media-tool and external-process settings separately.

    Keeping these controls in their own dialog prevents the general account,
    directory and proxy form from becoming taller than a typical screen.  The
    fields still write to the same config sections, so existing runtime
    behavior and configuration comments remain unchanged.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"扫描与外部工具 · V{APP_VERSION}")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        self.fields = {}

        def field(key: str, value, password=False):
            widget = QLineEdit(str(value if value is not None else ""))
            if password:
                widget.setEchoMode(QLineEdit.EchoMode.Password)
            self.fields[key] = widget
            return widget

        tool_box = QGroupBox("外部工具")
        tool_form = QFormLayout(tool_box)
        default_exiftool = "tools/exiftool.exe" if os.name == "nt" else "tools/exiftool"
        tool_form.addRow(
            "ExifTool 路径",
            field("exiftool_path", _cfg("EXIFTOOL_PATH", default_exiftool)),
        )
        tool_hint = QLabel(
            "ExifTool 用于读取视频内嵌日期；FFmpeg 用于视频信息、封面和必要的媒体日期回退。"
            "路径留空时将使用配置或随包提供的默认工具。"
        )
        tool_hint.setObjectName("mutedLabel")
        tool_hint.setWordWrap(True)
        tool_form.addRow("说明", tool_hint)
        content_layout.addWidget(tool_box)

        scan_box = QGroupBox("扫描稳定性与并发")
        scan_form = QFormLayout(scan_box)

        def integer_option(value, minimum, maximum, suffix=""):
            widget = QSpinBox()
            widget.setRange(minimum, maximum)
            widget.setValue(int(value))
            if suffix:
                widget.setSuffix(suffix)
            return widget

        def decimal_option(value, minimum, maximum, decimals=2, suffix=""):
            widget = QDoubleSpinBox()
            widget.setRange(minimum, maximum)
            widget.setDecimals(decimals)
            widget.setValue(float(value))
            if suffix:
                widget.setSuffix(suffix)
            return widget

        self.scan_stability_checks = integer_option(
            _cfg("SCAN_STABILITY_CHECKS", 2), 1, 8, " 次"
        )
        self.scan_stability_interval = decimal_option(
            _cfg("SCAN_STABILITY_INTERVAL_SECONDS", 0.05), 0.0, 5.0, 2, " 秒"
        )
        self.scan_stability_checks_local = integer_option(
            _cfg("SCAN_STABILITY_CHECKS_LOCAL", 2), 1, 8, " 次"
        )
        self.scan_stability_interval_local = decimal_option(
            _cfg("SCAN_STABILITY_INTERVAL_LOCAL_SECONDS", 0.05), 0.0, 30.0, 2, " 秒"
        )
        self.scan_stability_checks_network = integer_option(
            _cfg("SCAN_STABILITY_CHECKS_NETWORK", 3), 1, 8, " 次"
        )
        self.scan_stability_interval_network = decimal_option(
            _cfg("SCAN_STABILITY_INTERVAL_NETWORK_SECONDS", 0.5), 0.0, 60.0, 2, " 秒"
        )
        self.scan_discovery_attempts = integer_option(
            _cfg("SCAN_DISCOVERY_ATTEMPTS", 3), 1, 8, " 次"
        )
        self.scan_discovery_initial_delay = decimal_option(
            _cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15), 0.0, 10.0, 2, " 秒"
        )
        self.scan_discovery_max_delay = decimal_option(
            _cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0), 0.0, 60.0, 2, " 秒"
        )
        self.scan_readiness_attempts = integer_option(
            _cfg("SCAN_READINESS_ATTEMPTS", 3), 1, 8, " 次"
        )
        self.scan_probe_bytes = integer_option(
            _cfg("SCAN_READ_PROBE_BYTES", 65536), 1, 4 * 1024 * 1024, " 字节"
        )
        self.scan_workers_local = integer_option(
            _cfg("IO_WORKERS_LOCAL", 4), 1, 32, " 个"
        )
        self.scan_workers_network = integer_option(
            _cfg("IO_WORKERS_NETWORK", 2), 1, 16, " 个"
        )
        scan_form.addRow("稳定性检查次数", self.scan_stability_checks)
        scan_form.addRow("稳定性检查间隔", self.scan_stability_interval)
        scan_form.addRow("本地稳定检查次数", self.scan_stability_checks_local)
        scan_form.addRow("本地稳定检查间隔", self.scan_stability_interval_local)
        scan_form.addRow("网络稳定检查次数", self.scan_stability_checks_network)
        scan_form.addRow("网络稳定检查间隔", self.scan_stability_interval_network)
        scan_form.addRow("目录发现重试次数", self.scan_discovery_attempts)
        scan_form.addRow("发现首次等待", self.scan_discovery_initial_delay)
        scan_form.addRow("发现最大等待", self.scan_discovery_max_delay)
        scan_form.addRow("不可读重试次数", self.scan_readiness_attempts)
        scan_form.addRow("读探针大小", self.scan_probe_bytes)
        scan_form.addRow("本地 I/O 并发", self.scan_workers_local)
        scan_form.addRow("网络 I/O 并发", self.scan_workers_network)
        content_layout.addWidget(scan_box)

        process_box = QGroupBox("外部进程超时与批次")
        process_form = QFormLayout(process_box)
        self.process_timeouts = {}
        for key, label, config_key, default in (
            ("exiftool_timeout_seconds", "ExifTool 超时", "EXIFTOOL_TIMEOUT_SECONDS", 120),
            ("ffmpeg_metadata_timeout_seconds", "FFmpeg 日期超时", "FFMPEG_METADATA_TIMEOUT_SECONDS", 30),
            ("ffmpeg_info_timeout_seconds", "FFmpeg 信息超时", "FFMPEG_INFO_TIMEOUT_SECONDS", 30),
            ("ffmpeg_thumbnail_timeout_seconds", "FFmpeg 封面超时", "FFMPEG_THUMBNAIL_TIMEOUT_SECONDS", 45),
            ("ffmpeg_compression_timeout_seconds", "FFmpeg 压缩超时", "FFMPEG_COMPRESSION_TIMEOUT_SECONDS", 45),
        ):
            widget = decimal_option(_cfg(config_key, default), 1.0, 86400.0, 1, " 秒")
            self.process_timeouts[key] = widget
            process_form.addRow(label, widget)
        self.exiftool_batch_size = integer_option(
            _cfg("EXIFTOOL_BATCH_SIZE", 256), 1, 4096, " 个文件"
        )
        self.exiftool_retries = integer_option(
            _cfg("EXIFTOOL_RETRIES", 2), 0, 5, " 次"
        )
        process_form.addRow("ExifTool 批次大小", self.exiftool_batch_size)
        process_form.addRow("ExifTool 重试次数", self.exiftool_retries)
        content_layout.addWidget(process_box)

        hint = QLabel(
            "扫描设置会影响网络盘稳定性、发现重试和 I/O 并发；外部进程设置会限制单次 ExifTool/FFmpeg 调用。"
            "修改后需要重新扫描才会应用到新的扫描任务。"
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        content_layout.addWidget(hint)
        content_layout.addStretch(1)

        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(760, 700)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.setMaximumHeight(max(420, available.height() - 80))
            self.resize(
                min(self.width(), max(620, available.width() - 80)),
                min(self.height(), max(420, available.height() - 80)),
            )

    def _save(self):
        exiftool_path = self.fields["exiftool_path"].text().strip()
        validation_error = _validate_exiftool_path(exiftool_path)
        if validation_error:
            QMessageBox.warning(self, "ExifTool 路径不可用", validation_error)
            return
        values = {
            ("paths", "exiftool_path"): exiftool_path,
            ("scan", "stability_checks"): self.scan_stability_checks.value(),
            ("scan", "stability_interval_seconds"): self.scan_stability_interval.value(),
            ("scan", "stability_checks_local"): self.scan_stability_checks_local.value(),
            ("scan", "stability_interval_local_seconds"): self.scan_stability_interval_local.value(),
            ("scan", "stability_checks_network"): self.scan_stability_checks_network.value(),
            ("scan", "stability_interval_network_seconds"): self.scan_stability_interval_network.value(),
            ("scan", "discovery_attempts"): self.scan_discovery_attempts.value(),
            ("scan", "discovery_initial_delay_seconds"): self.scan_discovery_initial_delay.value(),
            ("scan", "discovery_max_delay_seconds"): self.scan_discovery_max_delay.value(),
            ("scan", "readiness_attempts"): self.scan_readiness_attempts.value(),
            ("scan", "read_probe_bytes"): self.scan_probe_bytes.value(),
            ("scan", "io_workers_local"): self.scan_workers_local.value(),
            ("scan", "io_workers_network"): self.scan_workers_network.value(),
        }
        values.update({("process", key): widget.value() for key, widget in self.process_timeouts.items()})
        values[("process", "exiftool_batch_size")] = self.exiftool_batch_size.value()
        values[("process", "exiftool_retries")] = self.exiftool_retries.value()
        error = _write_config_values(values)
        if error:
            QMessageBox.critical(self, "保存失败", error)
            return
        self.accept()


APP_STYLE = """
QMainWindow, QWidget { background: #10151d; color: #dbe5ef; font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 13px; }
QToolTip { background: #1b2734; color: #e8f0f7; border: 1px solid #3a4b5e; padding: 5px; }
QListWidget#sidebar { background: #0b1118; border: 0; border-right: 1px solid #263241; padding: 14px 8px; outline: 0; }
QListWidget#sidebar::item { padding: 12px 14px; margin: 3px 0; border-radius: 8px; color: #91a2b5; }
QListWidget#sidebar::item:hover { background: #172535; color: #dbe8f4; }
QListWidget#sidebar::item:selected { background: #285b91; color: #ffffff; }
QGroupBox { background: #141c26; border: 1px solid #2b3948; border-radius: 10px; margin-top: 10px; padding: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #a8b9cc; background: #141c26; }
QFrame#statCard { background: #17222e; border: 1px solid #2b3948; border-radius: 10px; }
QLabel { background: transparent; }
QLabel#pageTitle { font-size: 24px; font-weight: 700; color: #f0f5fa; }
QLabel#statValue { font-size: 20px; font-weight: 700; color: #f0f5fa; }
QLabel#statValue[good="true"] { color: #73d99a; }
QLabel#statValue[good="false"] { color: #f0c36b; }
QLabel#valueLabel { color: #eaf2f8; font-weight: 600; }
QLabel#mutedLabel { color: #91a2b5; }
QPushButton { min-height: 34px; padding: 0 16px; border-radius: 7px; border: 1px solid #354657; background: #1b2733; color: #dbe5ef; }
QPushButton:hover { background: #263747; border-color: #4b6175; }
QPushButton:pressed { background: #142331; }
QPushButton:disabled { color: #536171; background: #151d26; border-color: #263241; }
QPushButton#primaryButton { background: #237a4b; border-color: #31945d; color: white; font-weight: 600; }
QPushButton#primaryButton:hover { background: #2d9660; }
QPushButton#secondaryButton { background: #285f98; border-color: #3f7fbb; color: white; }
QPushButton#secondaryButton:hover { background: #3274b2; }
QPushButton#dangerButton { background: #a83d43; border-color: #cf5a5d; color: white; }
QPushButton#dangerButton:hover { background: #bd4a4f; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTreeWidget, QListWidget, QTableWidget { background: #0e141c; color: #dbe5ef; border: 1px solid #2b3948; border-radius: 7px; padding: 6px; selection-background-color: #2d6fa9; selection-color: #ffffff; }
QComboBox QAbstractItemView { background: #0e141c; color: #dbe5ef; border: 1px solid #2b3948; selection-background-color: #2d6fa9; selection-color: #ffffff; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTreeWidget:focus, QTableWidget:focus { border-color: #4d8fc5; }
QTreeWidget, QTableWidget { alternate-background-color: #151e29; gridline-color: #263241; }
QTreeWidget::item, QTableWidget::item { background: #0e141c; color: #dbe5ef; padding: 5px 4px; }
QTreeWidget::item:alternate, QTableWidget::item:alternate { background: #151e29; color: #dbe5ef; }
QTreeWidget::item:hover, QTableWidget::item:hover { background: #1c344a; color: #f5f9fc; }
QTreeWidget::item:selected, QListWidget::item:selected, QTableWidget::item:selected { background: #2d6fa9; color: #ffffff; }
QTreeWidget::branch { background: #0e141c; }
QHeaderView::section { background: #1b2734; color: #a8b9cc; border: 0; border-bottom: 1px solid #2b3948; padding: 7px; }
QTableCornerButton::section { background: #1b2734; border: 0; }
QProgressBar { background: #1b2733; border: 1px solid #2b3948; border-radius: 6px; height: 14px; text-align: center; color: #e7f0f7; }
QProgressBar::chunk { background: #2d9660; border-radius: 5px; }
QScrollBar:vertical { background: #0b1118; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #344657; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #4a6176; }
QScrollBar:horizontal { background: #0b1118; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #344657; border-radius: 5px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: #4a6176; }
QScrollBar::add-line, QScrollBar::sub-line { background: transparent; border: 0; }
QStatusBar { background: #0b1118; color: #91a2b5; border-top: 1px solid #263241; }
QMenu { background: #141c26; color: #dbe5ef; border: 1px solid #2b3948; }
QMenu::item:selected { background: #2d6fa9; color: #ffffff; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        application = QApplication.instance()
        if application is not None:
            self.setWindowIcon(application.windowIcon())
        self.setWindowTitle(f"TDLib Media Uploader · V{APP_VERSION} · Maximum 2026")
        self.setMinimumSize(860, 560)
        self.resize(1240, 800)
        self.worker: UploadWorker | None = None
        self.scanners: dict[str, ScanWorker] = {}
        self.active_kind = ""
        self.active_result = None
        self.started_at = ""
        self.auth_bridge = AuthBridge()
        self.auth_bridge.requested.connect(self._show_auth_dialog)
        self._build_ui()
        self._refresh_pages()

    def _upload_page(self, kind: str):
        """Return a media page after validating the explicit route kind."""

        return self.upload_pages[_require_kind(kind)]

    def _sidebar_row(self, kind: str) -> int:
        return self.sidebar_rows[_require_kind(kind)]

    def _build_ui(self):
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(215)
        for label in ("概览", "视频上传", "图片上传", "混合上传", "未确认上传", "任务中心", "历史记录", "设置与诊断"):
            self.sidebar.addItem(QListWidgetItem(label))
        root.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.home = HomePage()
        self.video_page = UploadPage("video")
        self.image_page = UploadPage("image")
        self.mixed_page = UploadPage("mixed")
        self.inflight_page = InflightPage()
        self.task_page = TaskPage()
        self.history_page = HistoryPage()
        self.settings_page = SettingsPage()
        self.upload_pages = {
            "video": self.video_page,
            "image": self.image_page,
            "mixed": self.mixed_page,
        }
        self.sidebar_rows = {"video": 1, "image": 2, "mixed": 3, "inflight": 4, "task": 5, "history": 6, "settings": 7}
        for page in (self.home, self.video_page, self.image_page, self.mixed_page, self.inflight_page, self.task_page, self.history_page, self.settings_page):
            if isinstance(page, (UploadPage, SettingsPage)):
                # Upload and settings pages contain several stacked sections.
                # Keeping both in a scroll area prevents controls and wrapped
                # diagnostic paths from being compressed or clipped when the
                # window is made shorter or narrower.
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QFrame.Shape.NoFrame)
                if isinstance(page, SettingsPage):
                    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    page.setMinimumWidth(0)
                    page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                scroll.setWidget(page)
                self.stack.addWidget(scroll)
            else:
                self.stack.addWidget(page)
        root.addWidget(self.stack, 1)

        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.sidebar.setCurrentRow(0)
        self.home.start_upload.connect(self._open_upload)
        self.home.open_settings.connect(lambda: self.sidebar.setCurrentRow(self.sidebar_rows["settings"]))
        for page in self.upload_pages.values():
            page.scan_requested.connect(self._scan)
            page.scan_cancel_requested.connect(self._stop_scan)
            page.start_requested.connect(self._start_upload)
            page.path_selected.connect(self._save_source_path)
            page.edit_target_requested.connect(self._edit_target)
        self.task_page.stop_requested.connect(self._stop_upload)
        self.inflight_page.reconciliation_requested.connect(self._reconcile_inflight)
        self.settings_page.open_editor.connect(self._edit_config)
        self.settings_page.open_scan_tools.connect(self._edit_scan_tools)
        self.settings_page.clear_all_requested.connect(self._clear_all_cache)
        self.settings_page.clear_thumb_requested.connect(self._clear_thumb_cache)

        self.statusBar().showMessage("就绪")

    def _refresh_pages(self):
        self.video_page.refresh_config()
        self.image_page.refresh_config()
        self.mixed_page.refresh_config()
        self.settings_page.refresh()
        self.inflight_page.reload_records()
        self.history_page.reload_records()
        self.inflight_page.reload_records()
        if _CONFIG_CREATED:
            self.statusBar().showMessage("已创建 config.toml，请先在设置中填写 Telegram 信息")

    def _open_upload(self, kind: str):
        self.sidebar.setCurrentRow(self._sidebar_row(kind))
        self._scan(kind)

    def _scan(self, kind: str):
        kind = _require_kind(kind)
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "任务运行中", "当前已有上传任务，请先安全停止后再扫描。")
            return
        if any(scanner.isRunning() for scanner in self.scanners.values()):
            QMessageBox.warning(self, "扫描运行中", "当前已有目录扫描，请等待扫描完成后再扫描另一个类型。")
            return
        old = self.scanners.get(kind)
        if old is not None and old.isRunning():
            return
        worker = ScanWorker(kind)
        self.scanners[kind] = worker
        page = self._upload_page(kind)
        page.set_scanning(True)
        worker.completed.connect(lambda result, k=kind: self._scan_done(k, result))
        worker.cancelled.connect(lambda result, k=kind: self._scan_done(k, result))
        worker.failed.connect(lambda message, k=kind: self._scan_failed(k, message))
        worker.progress_changed.connect(self._scan_progress)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @Slot(str)
    def _stop_scan(self, kind: str):
        kind = _require_kind(kind)
        scanner = self.scanners.get(kind)
        if scanner is None or not scanner.isRunning():
            return
        scanner.request_stop()
        page = self._upload_page(kind)
        page.status_label.setText("正在取消扫描…")
        self.statusBar().showMessage(f"正在取消{_kind_label(kind)}扫描…")

    @Slot(str, object)
    def _scan_progress(self, kind: str, payload: object):
        if not isinstance(payload, dict):
            return
        kind = _require_kind(kind)
        page = self._upload_page(kind)
        phase = str(payload.get("phase", "scan"))
        completed = max(0, int(payload.get("completed", 0) or 0))
        total = max(0, int(payload.get("total", 0) or 0))
        if phase == "exif":
            message = f"正在读取 ExifTool 日期… {completed}/{total}"
        elif phase == "media_date":
            message = f"正在读取媒体创建日期… {completed}/{total}"
        elif phase == "date_disabled":
            message = "日期读取已关闭，按文件名处理…"
        else:
            message = "正在扫描…"
        page.status_label.setText(message)
        self.statusBar().showMessage(message)

    def _scan_done(self, kind: str, result: dict):
        kind = _require_kind(kind)
        scanner = self.scanners.pop(kind, None)
        page = self._upload_page(kind)
        page.set_scanning(False)
        cancelled = bool(
            result.get("cancelled")
            or (scanner is not None and scanner.cancel_event.is_set())
        )
        if cancelled:
            page.set_cancelled(result)
            self.statusBar().showMessage(f"{_kind_label(kind)}扫描已取消")
        else:
            page.set_result(result)
            self.home.update_scan(result)
            self.statusBar().showMessage(f"{_kind_label(kind)}扫描完成")

    def _scan_failed(self, kind: str, message: str):
        kind = _require_kind(kind)
        self.scanners.pop(kind, None)
        page = self._upload_page(kind)
        page.set_scanning(False)
        page.status_label.setText(message)
        self.statusBar().showMessage(message)

    def _save_source_path(self, kind: str, path: str):
        kind = _require_kind(kind)
        if not self._can_change_configuration():
            self._upload_page(kind).refresh_config()
            return
        path = path.strip().strip('"')
        if not path or not Path(path).is_dir():
            self._upload_page(kind).refresh_config()
            QMessageBox.warning(self, "目录不可用", "请输入可访问的目录路径。")
            return
        normalized = _require_kind(kind)
        section_key = ("paths", KIND_PATH_CONFIG_KEYS[normalized])
        if path == str(_cfg(KIND_PATH_KEYS[normalized], "")):
            return
        error = _write_config_values({section_key: path})
        if error:
            QMessageBox.critical(self, "保存失败", error)
            self._upload_page(kind).refresh_config()
        else:
            self._invalidate_previews()
            self._refresh_pages()
            self.statusBar().showMessage("目录配置已保存")

    def _start_upload(self, kind: str):
        kind = _require_kind(kind)
        if any(scanner.isRunning() for scanner in self.scanners.values()):
            QMessageBox.information(self, "正在扫描", "请等待目录扫描完成后再上传。")
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "任务运行中", "图片和视频任务不能同时运行。")
            return
        page = self._upload_page(kind)
        result = page.result
        if not result or not result.get("pending_files"):
            QMessageBox.information(self, "无需上传", "当前没有待上传项目。")
            return
        if not result.get("core_available"):
            QMessageBox.warning(
                self,
                "依赖不完整",
                "当前环境只能预览，无法启动 TDLib 上传。请先按 README 的“从源码运行”说明安装依赖。",
            )
            return
        if _cfg("API_ID", 12345678) == 12345678 or _cfg("API_HASH", "YOUR_API_HASH") == "YOUR_API_HASH":
            QMessageBox.warning(self, "尚未配置", "请先在设置中填写 Telegram API ID 和 API Hash。")
            self.sidebar.setCurrentRow(self.sidebar_rows["settings"])
            return

        answer = QMessageBox.question(
            self,
            "确认开始上传",
            f"待上传 {result['pending_files']} 个文件，约 {_fmt_size(result['pending_bytes'])}，共 {result['album_count']} 组。\n"
            f"来源：{result.get('source_dir', '')}\n"
            f"目标 Chat ID：{_target_for(kind).get('chat_id', 0)}\n"
            + (f"话题 ID：{_target_for(kind).get('forum_topic_id', 0)}\n" if _target_for(kind).get('target_mode') != 'channel' else "目标类型：频道\n")
            + "\n确认开始？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.active_kind = kind
        self.active_result = result
        self.started_at = _dt.datetime.now().isoformat(timespec="seconds")
        self.task_page.start_session(kind, result)
        self.sidebar.setCurrentRow(self.sidebar_rows["task"])
        worker = UploadWorker(kind, self.auth_bridge)
        self.worker = worker
        worker.ui.message_added.connect(self.task_page.add_message)
        worker.ui.progress_changed.connect(self.task_page.show_progress)
        worker.ui.album_changed.connect(self.task_page.show_album)
        worker.ui.target_changed.connect(self._target_from_worker)
        worker.completed.connect(self._upload_finished)
        worker.finished.connect(lambda w=worker: self._worker_thread_finished(w))
        for upload_page in self.upload_pages.values():
            upload_page.set_running(True)
        self.home.task_value.setText(f"{_kind_label(kind)}上传中")
        self.home.set_connection("上传中", True)
        self.statusBar().showMessage("上传任务已启动")
        worker.start()

    def _target_from_worker(self, payload: dict):
        self.home.set_connection("已连接", True)
        kind = _require_kind(payload.get("kind") or self.active_kind)
        page = self._upload_page(kind)
        is_channel = str(payload.get("target_mode") or _target_for(kind).get("target_mode")) == "channel"
        page.chat_label.setText(
            f"{'频道' if is_channel else '超级群组'} · "
            f"{payload.get('chat_title')} ({payload.get('chat_id')})"
        )
        page.topic_label.setText(
            "不适用（频道不使用 Topic）"
            if is_channel
            else f"{payload.get('topic_name')} ({payload.get('topic_id')})"
        )
        self.statusBar().showMessage(
            f"目标已确认：{payload.get('chat_title')}"
            + ("（频道）" if is_channel else f" / {payload.get('topic_name')}")
        )

    def _stop_upload(self):
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self,
                "立即停止上传",
                "将立即取消当前文件/Album 的 TDLib 上传；未完整发送的 Album 不会写入断点，"
                "下次会重新处理。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.worker.request_stop()
                self.task_page.task_status.setText("正在立即停止…")
                self.statusBar().showMessage("正在立即停止上传任务…")

    def _upload_finished(self, success: bool, message: str):
        write_app_log(
            "INFO" if success else "ERROR",
            f"{self.active_kind} 任务结束：{message}",
            source=f"upload/{self.active_kind or 'app'}",
        )
        if self.active_result is not None:
            records = _load_history()
            records.append({
                "started_at": self.started_at,
                "finished_at": _dt.datetime.now().isoformat(timespec="seconds"),
                "kind": self.active_kind,
                "source_dir": self.active_result.get("source_dir", ""),
                "total_files": self.active_result.get("total_files", 0),
                "total_bytes": self.active_result.get("total_bytes", 0),
                "success": success,
                "message": message,
            })
            _save_history(records)

        self.task_page.finish_session(success, message)
        page = self._upload_page(self.active_kind)
        for upload_page in self.upload_pages.values():
            upload_page.set_running(False)
        page.clear_scan_result()
        self.home.task_value.setText("无")
        self.home.set_connection("已连接" if success else "未连接", success)
        self.statusBar().showMessage(message)
        self.history_page.reload_records()
        self.inflight_page.reload_records()

    def _worker_thread_finished(self, worker: UploadWorker):
        if self.worker is worker:
            self.worker = None
        worker.deleteLater()

    @Slot(object, bool)
    def _reconcile_inflight(self, record: object, sent: bool):
        """Apply a confirmed manual decision without querying Telegram."""

        if not isinstance(record, dict):
            return
        if (
            self.worker is not None
            and self.worker.isRunning()
        ) or any(scanner.isRunning() for scanner in self.scanners.values()):
            QMessageBox.warning(
                self,
                "任务运行中",
                "扫描或上传任务运行时不能处理未确认记录，请等待任务完成或安全停止后再试。",
            )
            return
        kind = str(record.get("kind", "")).strip().lower()
        album_key = str(record.get("album_key", ""))
        if kind not in MEDIA_KINDS or not album_key:
            QMessageBox.warning(self, "记录无效", "这条未确认记录缺少媒体类型或 Album 标识。")
            return
        try:
            from tdlib_common import TDJsonClient

            client = TDJsonClient.__new__(TDJsonClient)
            from upload_journal import InflightJournal

            client.inflight_journal = InflightJournal()
            target = record.get("target") if isinstance(record.get("target"), dict) else {
                key: record.get(key)
                for key in ("target_mode", "chat_id", "forum_topic_id", "channel_chat_id")
                if key in record
            }
            client.reconcile_inflight(
                album_key,
                sent=bool(sent),
                kind=kind,
                target=target or None,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "人工处理未完成",
                f"{exc}\n\n记录仍会保留，以避免重复上传。",
            )
            return
        self.inflight_page.reload_records()
        self.statusBar().showMessage("未确认上传记录已更新")

    def _cache_operation_allowed(self) -> bool:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "任务运行中", "上传任务运行时不能清理缓存，请先安全停止任务。")
            return False
        active_scans = [worker for worker in self.scanners.values() if worker.isRunning()]
        if active_scans:
            QMessageBox.warning(self, "扫描运行中", "目录扫描运行时不能清理缓存，请等待扫描完成。")
            return False
        return True

    def _finish_cache_clear(self, keys: tuple[str, ...], *, reset_scan: bool):
        removed, errors = _clear_cache(keys)
        self.settings_page.refresh()
        if reset_scan:
            for upload_page in self.upload_pages.values():
                upload_page.clear_scan_result()
            self.home.clear_scan()
            self.history_page.reload_records()
        if errors:
            detail = "\n".join(errors)
            QMessageBox.warning(self, "缓存清理未完成", f"部分项目无法删除：\n{detail}")
            self.statusBar().showMessage("缓存清理部分完成")
            return
        if removed:
            self.statusBar().showMessage("缓存清理完成")
            QMessageBox.information(self, "缓存清理完成", "已清理：" + "、".join(removed))
        else:
            self.statusBar().showMessage("没有发现可清理的缓存")
            QMessageBox.information(self, "缓存清理", "没有发现可清理的缓存。")

    def _clear_all_cache(self):
        if not self._cache_operation_allowed():
            return
        answer = QMessageBox.warning(
            self,
            "确认清理所有缓存",
            "将清空视频/图片/混合上传状态、标题、视频封面缓存、历史记录、未确认上传记录、暂存副本和运行日志，保留目录本身。\n\n"
            "config.toml 和 Telegram 登录数据库不会被删除。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._finish_cache_clear(ALL_CACHE_KEYS, reset_scan=True)

    def _clear_thumb_cache(self):
        if not self._cache_operation_allowed():
            return
        answer = QMessageBox.question(
            self,
            "确认清理视频封面",
            "只清空视频封面缓存，保留目录本身，不影响上传状态和历史记录。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._finish_cache_clear(("thumb_cache",), reset_scan=False)

    def _edit_target(self, kind: str = "video"):
        kind = _require_kind(kind)
        if not self._can_change_configuration():
            return
        dialog = TargetDialog(kind, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._invalidate_previews()
            self._refresh_pages()
            saved_kind = dialog.kind
            self.statusBar().showMessage(
                f"{_kind_label(saved_kind)}上传目标已保存"
            )

    def _edit_config(self):
        if not self._can_change_configuration():
            return
        dialog = ConfigDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._invalidate_previews()
            self._refresh_pages()
            self.statusBar().showMessage("配置已保存")

    def _edit_scan_tools(self):
        if not self._can_change_configuration():
            return
        dialog = ScanToolsDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._invalidate_previews()
            self._refresh_pages()
            self.statusBar().showMessage("扫描与外部工具设置已保存")

    def _can_change_configuration(self):
        if (self.worker is not None and self.worker.isRunning()) or any(scanner.isRunning() for scanner in self.scanners.values()):
            QMessageBox.information(self, "任务进行中", "请等待扫描完成或停止上传后再修改配置。")
            return False
        return True

    def _invalidate_previews(self):
        for upload_page in self.upload_pages.values():
            upload_page.clear_scan_result()
        self.home.clear_scan()

    @Slot(str, bool)
    def _show_auth_dialog(self, prompt: str, password: bool):
        echo = QLineEdit.EchoMode.Password if password else QLineEdit.EchoMode.Normal
        value, accepted = QInputDialog.getText(self, "Telegram 登录", prompt, echo)
        if accepted and value.strip():
            self.auth_bridge.answer(value.strip())
        else:
            self.auth_bridge.answer("")
            if self.worker is not None:
                self.worker.request_stop()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self,
                "任务运行中",
                "上传任务仍在运行。是否立即停止上传并退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.request_stop()
            if not self.worker.wait(10000):
                QMessageBox.warning(self, "仍在运行", "TDLib 尚未结束，请稍后再关闭窗口。")
                event.ignore()
                return
        event.accept()


def main() -> int:
    if "--self-test" in sys.argv[1:]:
        return run_self_test()
    try:
        ensure_data_dirs()
    except OSError as exc:
        print(f"无法初始化数据目录：{exc}", file=sys.stderr)
        return 1
    _prepare_windows_app_identity()
    _prepare_qt_plugins()
    app = QApplication(sys.argv)
    instance_lock = InstanceLock(DATA_DIR / "app.lock")
    if not instance_lock.acquire():
        QMessageBox.warning(app.activeWindow(), "程序已在运行", "TDLib Media Uploader 已在运行。")
        app.quit()
        return 1
    try:
        write_app_log("INFO", f"启动 TDLib Media Uploader V{APP_VERSION}", source="startup")
        app.setApplicationName("TDLib Media Uploader")
        app.setApplicationVersion(APP_VERSION)
        app.setWindowIcon(_application_icon())
        app.setStyle("Fusion")
        app.setStyleSheet(APP_STYLE)
        window = MainWindow()
        window.setWindowIcon(app.windowIcon())
        window.show()
        return app.exec()
    finally:
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
