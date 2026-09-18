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
import tomllib
from collections.abc import Mapping
from collections import defaultdict
from pathlib import Path, PureWindowsPath


if __name__ == "__main__" and not getattr(sys, "frozen", False):
    print(
        "此应用仅支持从发布包运行，请从 GitHub Releases 下载对应平台的程序包。",
        file=sys.stderr,
    )
    raise SystemExit(2)

from ..core.album import (
    CaptionLimitError,
    CaptionStore,
    album_key,
    compose_caption,
    validate_caption,
    with_filename_description,
)
from ..core.logging import APP_LOG_PATH, LOG_DIR, TDLIB_LOG_PATH, write_app_log
from ..core.filesystem_legacy import (
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
from PySide6.QtCore import QLibraryInfo, QTimer, Signal, Slot, Qt, QLockFile
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
from ..config.paths import (
    APP_DATA_DIR, CONFIG_PATH, RESOURCE_DIR, TEMPLATE_CONFIG_PATH,
    DATA_DIR, VIDEO_STATE_DIR, IMAGE_STATE_DIR, MIXED_STATE_DIR,
    CAPTIONS_DIR, UPLOAD_INFLIGHT_DIR, THUMBNAIL_CACHE_DIR,
    IMAGE_COMPRESSION_CACHE_DIR, STAGING_CACHE_DIR,
    HISTORY_PATH as RUNTIME_HISTORY_PATH,
    TDLIB_DATABASE_DIR as RUNTIME_TDLIB_DATABASE_DIR,
    TDLIB_FILES_DIR as RUNTIME_TDLIB_FILES_DIR,
    read_version, ensure_data_dirs,
)
from ..core.instance_lock import InstanceLock
from ..core.self_test import run_self_test
from .events import AuthBridge, GuiConsoleUI
from .models import (
    caption_payload as _v2_caption_payload,
    group_key as _v2_group_key,
    item_dict as _v2_item_dict,
    item_identity as _v2_item_identity,
    plan_dict as _v2_plan_dict,
    scan_result as _translate_v2_scan_result,
)
from .pages import (
    HomePage,
    ImagePage as _PackageImagePage,
    MixedPage as _PackageMixedPage,
    TaskPage,
    UploadPage as _PackageUploadPage,
    UploadPageServices,
    VideoPage as _PackageVideoPage,
)
from .workers import ScanWorker, UploadWorker


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
    # a user config.  The config loader loads the bundled template in memory for
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
    from ..config import loader as cfg
except Exception as exc:  # The settings page can still explain the problem.
    cfg = None
    _CONFIG_ERROR = str(exc)


def _reload_config() -> str:
    global cfg, _CONFIG_ERROR
    try:
        if "tdlib_media_uploader.config.loader" in sys.modules:
            cfg = importlib.reload(sys.modules["tdlib_media_uploader.config.loader"])
        else:
            cfg = importlib.import_module("tdlib_media_uploader.config.loader")
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


def _source_root_for(kind: str) -> Path:
    """Return the configured source root for a worker boundary call."""

    normalized = _require_kind(kind)
    return Path(_cfg(KIND_PATH_KEYS[normalized], PROJECT_DIR))


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
                return "找不到 config.toml 和默认配置资源。"
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


def _legacy_scan_result(kind: str, progress_callback=None, cancel_event=None) -> dict:
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
            from . import legacy_video as core_module
        elif kind == "image":
            from . import legacy_image as core_module
        elif kind == "mixed":
            from . import legacy_mixed as core_module
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


def _load_v2_gui_integration():
    """Load the package GUI boundary from the bundled application."""

    from .integration import (  # noqa: PLC0415
        V2IntegrationUnavailable,
        run_v2_upload,
        scan_v2,
    )
    return V2IntegrationUnavailable, run_v2_upload, scan_v2


def _v2_scan_result(bundle, *, progress_callback=None, cancel_event=None) -> dict:
    """Translate V2 scan/plan models into the existing preview page contract."""
    return _translate_v2_scan_result(
        bundle,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        cancelled_result_factory=_cancelled_scan_result,
        size_resolver=_item_size,
        logger=write_app_log,
        caption_store_factory=CaptionStore,
    )


def _scan_result(kind: str, progress_callback=None, cancel_event=None) -> dict:
    """Run the V2 strategy scan, retaining the old preview fallback."""

    kind = _require_kind(kind)
    if cfg is None:
        raise RuntimeError(_CONFIG_ERROR or "配置不可用。")
    try:
        unavailable, _run_v2_upload, scan_v2 = _load_v2_gui_integration()
        from ..config import paths as _runtime_paths  # noqa: PLC0415

        bundle = scan_v2(
            kind,
            source_root=Path(_cfg(KIND_PATH_KEYS[kind], PROJECT_DIR)),
            target=_target_for(kind),
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            config=cfg,
            runtime_paths=_runtime_paths,
        )
        return _v2_scan_result(bundle, progress_callback=progress_callback, cancel_event=cancel_event)
    except unavailable:
        return _legacy_scan_result(
            kind,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )


from .dialogs import ConfigDialog, ScanToolsDialog, TargetDialog
from .pages import (
    HistoryPage,
    InflightPage,
    SettingsPage,
)
from .components import NavigationSidebar
from .theme import APP_STYLE, THEME


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


def _upload_page_services() -> UploadPageServices:
    """Inject legacy globals while the package owns the upload page widget."""

    return UploadPageServices(
        require_kind=_require_kind,
        kind_label=_kind_label,
        config_getter=_cfg,
        target_getter=_target_for,
        path_keys=KIND_PATH_KEYS,
        project_dir=PROJECT_DIR,
        path_text=_path_text,
        size_formatter=_fmt_size,
        item_size=_item_size,
        stable_path=stable_path,
        filename_description=with_filename_description,
        compose_caption=compose_caption,
        validate_caption=validate_caption,
        caption_store_factory=CaptionStore,
        caption_limit=CAPTION_EDITOR_SOFT_LIMIT,
        dialog_class=QDialog,
        message_box_class=QMessageBox,
        file_dialog_class=QFileDialog,
    )


class UploadPage(_PackageUploadPage):
    """Bridge legacy root references to the package-owned upload widget."""

    def __init__(self, kind: str, *, services: UploadPageServices | None = None):
        super().__init__(kind, services=services or _upload_page_services())


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

        self.nav_sidebar = NavigationSidebar(version=APP_VERSION)
        self.sidebar = self.nav_sidebar.list
        root.addWidget(self.nav_sidebar)

        self.stack = QStackedWidget()
        self.home = HomePage()
        page_services = _upload_page_services()
        self.video_page = _PackageVideoPage(services=page_services)
        self.image_page = _PackageImagePage(services=page_services)
        self.mixed_page = _PackageMixedPage(services=page_services)
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
            if isinstance(page, (_PackageUploadPage, SettingsPage)):
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
        self.task_page.safe_stop_requested.connect(self._safe_stop_upload)
        self.task_page.immediate_stop_requested.connect(self._immediate_stop_upload)
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
        worker = ScanWorker(kind, scan_runner=_scan_result)
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
                "当前环境只能预览，无法启动 TDLib 上传。请从 GitHub Releases 下载完整发布包。",
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
        worker = UploadWorker(
            kind,
            self.auth_bridge,
            result,
            target_provider=_target_for,
            source_root_provider=_source_root_for,
            config=cfg,
        )
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
        self.nav_sidebar.set_connection_status(True, "上传中")
        self.statusBar().showMessage("上传任务已启动")
        worker.start()

    def _target_from_worker(self, payload: dict):
        self.home.set_connection("已连接", True)
        self.nav_sidebar.set_connection_status(True, "已连接")
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

    def _safe_stop_upload(self):
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self,
                "安全停止上传",
                "当前正在发送的 Album 会继续完成并保存断点；完成后不再开始新的 Album。"
                "是否安全停止？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.worker.request_safe_stop()
                self.task_page.task_status.setText("当前 Album 完成后安全停止…")
                self.statusBar().showMessage("已请求安全停止，当前 Album 将继续完成…")

    def _immediate_stop_upload(self):
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.warning(
                self,
                "立即中断上传",
                "将立即请求取消当前 TDLib 上传。当前 Album 可能已经部分或全部提交给 Telegram，"
                "结果可能进入“未确认上传”，需要人工核对后才能继续。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.worker.request_immediate_stop()
                self.task_page.task_status.setText("正在立即中断…")
                self.statusBar().showMessage("正在立即中断上传任务；当前 Album 可能进入 UNKNOWN…")

    def _stop_upload(self):
        """Compatibility alias for older integrations; uses safe stop."""

        self._safe_stop_upload()

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
        self.nav_sidebar.set_connection_status(success, "已连接" if success else "未连接")
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
            from tdlib_media_uploader.upload.reconciliation import ReconciliationService

            client = ReconciliationService()
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
    if not getattr(sys, "frozen", False):
        print(
            "此应用仅支持从发布包运行，请从 GitHub Releases 下载对应平台的程序包。",
            file=sys.stderr,
        )
        return 2
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
