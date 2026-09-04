# -*- coding: utf-8 -*-
"""PySide6 desktop interface for TDLib Media Uploader V1.8.2.

The GUI is the only user-facing interface.  Upload cores remain the source of
truth for scanning, Album creation, TDLib requests and resumable state.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import importlib.util
import json
import re
import shutil
import sys
import threading
from collections import defaultdict
from pathlib import Path

from album_metadata import CaptionStore, album_key, compose_caption, with_filename_description
from path_utils import file_mtime, iter_files, stable_path
from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "config.toml"
TEMPLATE_CONFIG_PATH = PROJECT_DIR / "config.example.toml"
HISTORY_PATH = PROJECT_DIR / ".gui_history.json"
APP_VERSION = "1.8.2"
ICON_PATH = PROJECT_DIR / "assets" / "tdlib_media_uploader_icon.ico"


def _ensure_config_file() -> bool:
    if CONFIG_PATH.exists() or not TEMPLATE_CONFIG_PATH.exists():
        return False
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
    if cfg is not None and callable(getattr(cfg, "target_for", None)):
        return cfg.target_for(kind)
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


def _fmt_date(value) -> str:
    if value is None:
        return "—"
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _path_text(value) -> str:
    return str(value) if value is not None else ""


def _basic_paths(kind: str) -> list[Path]:
    root = Path(_cfg("VIDEO_DIR" if kind == "video" else "IMAGE_DIR", PROJECT_DIR))
    extensions = set(_cfg("VIDEO_EXTENSIONS" if kind == "video" else "IMAGE_EXTENSIONS", set()))
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"{kind} 目录不存在或不是目录：{root}")
    paths, _errors = iter_files(root, extensions)
    if kind == "image" and _cfg("IMAGE_SORT_MODE", "mtime") == "mtime":
        paths.sort(key=lambda item: (file_mtime(item), item.name.lower()))
    else:
        paths.sort(key=lambda item: str(item).lower())
    return paths


def _item_size(item) -> int:
    path = item["path"] if isinstance(item, dict) else item
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _update_toml_value(text: str, section: str, key: str, value) -> str:
    if isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, int):
        literal = str(value)
    else:
        literal = json.dumps(str(value), ensure_ascii=False)

    section_re = re.compile(
        rf"(?ms)^(\[{re.escape(section)}\]\s*$)(.*?)(?=^\[|\Z)"
    )
    match = section_re.search(text)
    if not match:
        suffix = "\n" if text and not text.endswith("\n") else ""
        return f"{text}{suffix}\n[{section}]\n{key} = {literal}\n"

    body = match.group(2)
    key_re = re.compile(rf"(?m)^(\s*{re.escape(key)}\s*=\s*).*$")
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
            shutil.copyfile(TEMPLATE_CONFIG_PATH, CONFIG_PATH)
        text = CONFIG_PATH.read_text(encoding="utf-8")
        for (section, key), value in values.items():
            text = _update_toml_value(text, section, key, value)
        CONFIG_PATH.write_text(text, encoding="utf-8")
        return _reload_config()
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


CACHE_TARGETS = {
    "video_state": ("视频上传状态", PROJECT_DIR / ".video_state"),
    "legacy_video_state": ("旧版视频状态", PROJECT_DIR / ".state"),
    "image_state": ("图片上传状态", PROJECT_DIR / ".image_state"),
    "thumb_cache": ("视频封面缓存", PROJECT_DIR / ".thumb_cache"),
    "video_album_captions": ("视频 Album 标题", PROJECT_DIR / ".video_album_captions.json"),
    "image_album_captions": ("图片 Album 标题", PROJECT_DIR / ".image_album_captions.json"),
    "gui_history": ("GUI 历史记录", HISTORY_PATH),
}
ALL_CACHE_KEYS = tuple(CACHE_TARGETS)


def _cache_usage(path: Path) -> tuple[int, int]:
    """Return file count and byte size without following a directory symlink."""
    try:
        if path.is_symlink():
            return (1, path.lstat().st_size)
        if path.is_file():
            return (1, path.stat().st_size)
        if not path.is_dir():
            return (0, 0)
        count = 0
        total = 0
        for child in path.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    count += 1
                    total += child.stat().st_size
            except OSError:
                continue
        return (count, total)
    except OSError:
        return (0, 0)


def _cache_status_text() -> str:
    rows = []
    for label, path in CACHE_TARGETS.values():
        if not (path.exists() or path.is_symlink()):
            continue
        count, total = _cache_usage(path)
        rows.append(f"{label} {count} 项 · {_fmt_size(total)}")
    return "当前应用缓存：" + (" · ".join(rows) if rows else "无")


def _remove_cache_path(path: Path) -> None:
    """Clear a known cache path while keeping its directory structure."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        for child in path.iterdir():
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                _remove_cache_path(child)


def _clear_cache(keys: tuple[str, ...]) -> tuple[list[str], list[str]]:
    removed = []
    errors = []
    for key in keys:
        label, path = CACHE_TARGETS[key]
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            _remove_cache_path(path)
            removed.append(label)
        except OSError as exc:
            errors.append(f"{label}：{exc}")
    return removed, errors


def _scan_result(kind: str) -> dict:
    """Scan using the existing core when available, with a preview fallback."""
    if cfg is None:
        raise RuntimeError(_CONFIG_ERROR or "配置不可用。")
    activate = getattr(cfg, "activate_target", None)
    if callable(activate):
        activate(kind)

    core = None
    warning = ""
    scan_errors: list[str] = []
    try:
        if kind == "video":
            import tdlib_video_album_uploader as core_module
        else:
            import tdlib_image_album_uploader as core_module
        core = core_module
    except Exception as exc:
        warning = f"当前环境尚未加载完整上传依赖，预览使用基础扫描：{exc}"

    state = None
    if kind == "video":
        if core is not None:
            core.STATE_DIR = PROJECT_DIR / ".video_state"
            paths = core.scan_videos()
            scan_errors = list(getattr(core, "LAST_SCAN_ERRORS", []))
            metadata = {}
            exiftool = Path(_cfg("EXIFTOOL_PATH", ""))
            if exiftool.exists():
                metadata = core.read_exif_metadata()
            items, missing = core.build_items(paths, metadata)
            state = core.UploadState()
        else:
            paths = _basic_paths(kind)
            missing = []
            items = [
                {
                    "path": path,
                    "capture_time": _dt.datetime.fromtimestamp(file_mtime(path)),
                    "month_key": _dt.datetime.fromtimestamp(file_mtime(path)).strftime("%Y-%m"),
                    "date_tag": "FileSystem:ModifyTime",
                    "fallback": True,
                }
                for path in paths
            ]
    else:
        if core is not None:
            paths = core.scan_images()
            scan_errors = list(getattr(core, "LAST_SCAN_ERRORS", []))
            state = core.UploadState()
        else:
            paths = _basic_paths(kind)
        items = paths
        missing = []

    def completed(item) -> bool:
        path = item["path"] if isinstance(item, dict) else item
        return bool(state is not None and state.is_completed(path))

    groups = []
    if kind == "video":
        force_ten = bool(_cfg("VIDEO_FORCE_TEN_PER_ALBUM", False))
        forced_key = getattr(core, "FORCED_GROUP_KEY", "__all_videos__")
        if force_ten:
            grouped = {forced_key: list(items)}
        else:
            grouped = defaultdict(list)
            for item in items:
                grouped[item["month_key"]].append(item)
        album_size = 10 if force_ten else int(_cfg("VIDEO_ALBUM_SIZE", 10))
        for month in sorted(grouped):
            month_items = grouped[month]
            if core is not None:
                plans = core.build_album_plans(month_items, state)
            else:
                plans = []
                for offset in range(0, len(month_items), album_size):
                    album_items = list(month_items[offset:offset + album_size])
                    default_caption = (
                        f"Album {offset // album_size + 1}"
                        if force_ten
                        else month[2:].lstrip("0")
                    )
                    key_group = f"{month}:{offset // album_size + 1}" if force_ten else month
                    key = album_key("video", key_group, album_items)
                    record = CaptionStore("video").get(key, default_caption)
                    plans.append({
                        "key": key,
                        "month_key": month,
                        "number": offset // album_size + 1,
                        "items": album_items,
                        "pending_items": [item for item in album_items if not completed(item)],
                        "caption": {
                            "base_label": record["base_label"],
                            "custom_text": record["custom_text"],
                            "text": compose_caption(record["base_label"], record["custom_text"], " · "),
                        },
                    })
            pending = [item for item in month_items if not completed(item)]
            pending_plans = [plan for plan in plans if plan["pending_items"]]
            group_label = (
                getattr(core, "group_display_name", lambda value: value)(month)
                if core is not None
                else ("全部视频（忽略日期）" if force_ten else month)
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
    else:
        album_size = int(_cfg("IMAGE_ALBUM_SIZE", 10))
        if core is not None:
            plans = core.build_album_plans(items, state)
        else:
            plans = []
            for offset in range(0, len(items), album_size):
                album_items = list(items[offset:offset + album_size])
                number = offset // album_size + int(_cfg("IMAGE_ALBUM_NUMBER_START", 1))
                key = album_key("image", f"Album {number}", album_items)
                record = CaptionStore("image").get(key, str(number))
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
        warning = (
            f"扫描时跳过 {len(scan_errors)} 个暂时无法读取的项目（可能是 SMB 连接中断）。"
            + (f"；{warning}" if warning else "")
        )

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
        "source_dir": str(_cfg("VIDEO_DIR" if kind == "video" else "IMAGE_DIR", "")),
        "state_path": str(state.path) if state is not None else "",
        "core_available": core is not None,
        "warning": warning,
        "target": _target_for(kind),
    }


class ScanWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind

    def run(self):
        try:
            self.completed.emit(_scan_result(self.kind))
        except Exception as exc:
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
        self.message_added.emit(level, str(text))

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
        self.kind = kind
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

                core.STATE_DIR = PROJECT_DIR / ".video_state"
                core.UI = self.ui
                entry.UI = self.ui
                entry.main()
            else:
                import tdlib_image_album_uploader as core

                core.UI = self.ui
                core.main()
            if self.ui.stop_requested:
                self.completed.emit(False, "任务已立即停止；完整完成的 Album 已保存断点。")
            else:
                self.completed.emit(True, "上传任务完成。")
        except Exception as exc:
            if type(exc).__name__ == "TDLibCancelled" or self.ui.stop_requested:
                self.completed.emit(False, "任务已立即停止；完整完成的 Album 已保存断点。")
            else:
                self.ui.error(f"程序停止：{type(exc).__name__}: {exc}")
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
        settings = QPushButton("配置与诊断")
        settings.setObjectName("secondaryButton")
        video.clicked.connect(lambda: self.start_upload.emit("video"))
        image.clicked.connect(lambda: self.start_upload.emit("image"))
        settings.clicked.connect(self.open_settings)
        task_layout.addWidget(video)
        task_layout.addWidget(image)
        task_layout.addWidget(settings)
        task_layout.addStretch(1)
        layout.addWidget(task_box)

        note = QGroupBox("V1.8.2 运行提示")
        note_layout = QVBoxLayout(note)
        note_body = QLabel(
            "GUI 与上传核心共用 TDLib 登录数据和断点文件。一次只能运行一个图片或视频任务；"
            "图片 Album 默认按组编号，视频 Album 保留日期标题；双击预览中的 Album 可编辑标题。"
            "视频和图片页面分别使用各自的 Telegram 目标和媒体选项，未单独配置目标时继承公共目标。"
            "频道模式不使用 Topic；点击“安全停止”会立即取消正在上传的文件，完整发送成功的 Album 会在下次自动跳过。"
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
    start_requested = Signal(str)
    path_selected = Signal(str, str)
    scan_requested = Signal(str)
    edit_target_requested = Signal(str)

    def __init__(self, kind: str):
        super().__init__()
        self.kind = kind
        self.result = None
        self._running = False
        accent = "视频" if kind == "video" else "图片"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        title = QLabel(f"{accent}上传")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        source_box = QGroupBox("1 · 来源目录")
        source_layout = QHBoxLayout(source_box)
        self.source_edit = QLineEdit()
        self.source_edit.setReadOnly(True)
        browse = QPushButton("选择目录")
        browse.clicked.connect(self._browse)
        source_layout.addWidget(self.source_edit, 1)
        source_layout.addWidget(browse)
        layout.addWidget(source_box)

        preview_box = QGroupBox("2 · 扫描与 Album 预览")
        preview_layout = QVBoxLayout(preview_box)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["状态", "日期 / 分组", "大小", "文件"])
        self.tree.header().setStretchLastSection(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        # Keep the preview useful on a normal window while allowing the page
        # scroll area to reveal the target/actions on short windows.
        self.tree.setMinimumHeight(220)
        self.tree.itemDoubleClicked.connect(self._edit_album)
        preview_layout.addWidget(self.tree)
        self.summary_label = QLabel("尚未扫描")
        self.summary_label.setObjectName("mutedLabel")
        preview_layout.addWidget(self.summary_label)
        layout.addWidget(preview_box, 1)

        target_box = QGroupBox(f"3 · Telegram 目标（{accent}上传）")
        target_layout = QGridLayout(target_box)
        target_layout.addWidget(QLabel(f"{accent}目标"), 0, 0)
        target_layout.addWidget(QLabel("Forum Topic"), 1, 0)
        self.chat_label = QLabel("未配置")
        self.topic_label = QLabel("未配置")
        self.chat_label.setObjectName("valueLabel")
        self.topic_label.setObjectName("valueLabel")
        target_layout.addWidget(self.chat_label, 0, 1)
        target_layout.addWidget(self.topic_label, 1, 1)
        edit_target = QPushButton(f"编辑{accent}目标与配置")
        edit_target.setObjectName("secondaryButton")
        edit_target.clicked.connect(lambda: self.edit_target_requested.emit(self.kind))
        target_layout.addWidget(edit_target, 0, 2, 2, 1)
        layout.addWidget(target_box)

        bottom = QHBoxLayout()
        self.status_label = QLabel("准备扫描")
        self.status_label.setObjectName("mutedLabel")
        self.scan_button = QPushButton("扫描目录")
        self.scan_button.setObjectName("secondaryButton")
        self.scan_button.clicked.connect(lambda: self.scan_requested.emit(self.kind))
        self.start_button = QPushButton("开始上传")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(lambda: self.start_requested.emit(self.kind))
        bottom.addWidget(self.status_label)
        bottom.addStretch(1)
        bottom.addWidget(self.scan_button)
        bottom.addWidget(self.start_button)
        layout.addLayout(bottom)
        self.refresh_config()

    def refresh_config(self):
        path_name = "VIDEO_DIR" if self.kind == "video" else "IMAGE_DIR"
        self.source_edit.setText(_path_text(_cfg(path_name, "")))
        target = _target_for(self.kind)
        mode = str(target.get("target_mode", "forum_topic"))
        target_name = "频道" if mode == "channel" else "超级群组"
        self.chat_label.setText(f"{target_name} · {str(target.get('chat_id', '未配置'))}")
        self.topic_label.setText(
            "不适用（频道不使用 Topic）"
            if mode == "channel"
            else str(target.get("forum_topic_id", "未配置"))
        )

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择目录", self.source_edit.text() or str(PROJECT_DIR))
        if path:
            self.source_edit.setText(path)
            self.path_selected.emit(self.kind, path)

    def set_scanning(self, active: bool):
        self.scan_button.setEnabled(not active and not self._running)
        if active:
            self.status_label.setText("正在扫描…")

    def set_result(self, result: dict):
        self.result = result
        self.tree.clear()
        completed_paths = set(result.get("completed_paths", []))
        for group in result["groups"]:
            label = (
                f"{group['label']} · {len(group['items'])} 个 · "
                f"已完成 {group['completed']} · 待上传 {group['pending']} · "
                f"{group['albums']} 个 Album"
            )
            top = QTreeWidgetItem(["分组", group["label"], "", label])
            top.setExpanded(True)
            self.tree.addTopLevelItem(top)
            plans = group.get("album_plans") or [{
                "key": "",
                "number": 1,
                "items": group["items"],
                "pending_items": [item for item in group["items"] if stable_path(item["path"] if isinstance(item, dict) else item) not in completed_paths],
                "caption": {"text": group.get("caption", ""), "base_label": group.get("caption", ""), "custom_text": ""},
            }]
            for plan in plans:
                album_items = plan.get("items", [])
                pending_count = len(plan.get("pending_items", []))
                completed_count = len(album_items) - pending_count
                caption_text = with_filename_description(
                    plan.get("caption", {}).get("text", ""),
                    album_items,
                    bool(_cfg(
                        "VIDEO_CAPTION_INCLUDE_FILENAMES"
                        if self.kind == "video"
                        else "IMAGE_CAPTION_INCLUDE_FILENAMES",
                        False,
                    )),
                )
                album_row = QTreeWidgetItem([
                    "Album" if pending_count else "已完成",
                    f"Album {plan.get('number', 1)} · {caption_text or '无 Caption'}",
                    _fmt_size(sum(_item_size(item) for item in album_items)),
                    f"{len(album_items)} 个文件 · 已完成 {completed_count} · 待上传 {pending_count}",
                ])
                album_row.setData(0, Qt.ItemDataRole.UserRole, plan)
                top.addChild(album_row)
                for item in album_items:
                    path = item["path"] if isinstance(item, dict) else item
                    completed = stable_path(path) in completed_paths
                    date_value = item.get("capture_time") if isinstance(item, dict) else None
                    row = QTreeWidgetItem([
                        "待上传" if not completed else "已完成",
                        _fmt_date(date_value),
                        _fmt_size(_item_size(item)),
                        str(path),
                    ])
                    album_row.addChild(row)
        self.summary_label.setText(
            f"共 {result['total_files']} 个 · {_fmt_size(result['total_bytes'])} · "
            f"已完成 {result['completed_files']} · 待上传 {result['pending_files']} · "
            f"{result['album_count']} 个 Album"
        )
        if result.get("missing"):
            self.summary_label.setText(self.summary_label.text() + f" · 缺失日期 {len(result['missing'])}")
        if result.get("warning"):
            self.status_label.setText(result["warning"])
        elif result["pending_files"] == 0:
            self.status_label.setText("全部项目已在断点记录中")
        elif not result["core_available"]:
            self.status_label.setText("预览可用；安装完整依赖后才能上传")
        else:
            self.status_label.setText("扫描完成，可开始上传")
        self.start_button.setEnabled(bool(result["pending_files"] and result["core_available"] and not self._running))

    def _edit_album(self, item, _column=0):
        plan = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(plan, dict) or not plan.get("key"):
            return
        current = plan.get("caption") or {}
        base_label = str(current.get("base_label", ""))
        custom_text = str(current.get("custom_text", ""))
        if self.kind == "video":
            base_label, accepted = QInputDialog.getText(
                self,
                "编辑视频 Album 标题",
                "基础标题（例如 25-1）：",
                QLineEdit.EchoMode.Normal,
                base_label,
            )
            if not accepted:
                return
        custom_text, accepted = QInputDialog.getText(
            self,
            "编辑 Album 自定义文本",
            "追加文本（留空表示不追加）：",
            QLineEdit.EchoMode.Normal,
            custom_text,
        )
        if not accepted:
            return
        store = CaptionStore(self.kind)
        store.set(plan["key"], base_label=base_label, custom_text=custom_text)
        separator = str(_cfg("VIDEO_ALBUM_CAPTION_SEPARATOR" if self.kind == "video" else "IMAGE_ALBUM_CAPTION_SEPARATOR", " · "))
        plan["caption"] = {
            "base_label": base_label,
            "custom_text": custom_text,
            "text": compose_caption(base_label if (self.kind == "video" or _cfg("IMAGE_ALBUM_NUMBERING", True)) else "", custom_text, separator),
        }
        self.set_result(self.result)
        self.status_label.setText("Album 标题已保存")

    def clear_scan_result(self):
        self.result = None
        self.tree.clear()
        self.summary_label.setText("缓存已清理，请重新扫描目录")
        self.status_label.setText("等待重新扫描")
        self.start_button.setEnabled(False)

    def set_running(self, active: bool):
        self._running = active
        self.scan_button.setEnabled(not active)
        self.start_button.setEnabled(not active and bool(self.result and self.result.get("pending_files") and self.result.get("core_available")))
        if active:
            self.status_label.setText("任务运行中，请在任务中心查看进度")


class TaskPage(QWidget):
    stop_requested = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        title_row = QHBoxLayout()
        self.title = QLabel("任务中心")
        self.title.setObjectName("pageTitle")
        self.task_status = QLabel("无正在运行的任务")
        self.task_status.setObjectName("mutedLabel")
        title_row.addWidget(self.title)
        title_row.addStretch(1)
        title_row.addWidget(self.task_status)
        layout.addLayout(title_row)

        progress_box = QGroupBox("当前 Album")
        progress_layout = QVBoxLayout(progress_box)
        self.album_label = QLabel("尚未开始")
        self.album_label.setObjectName("valueLabel")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.metrics = QLabel("速度 — · Mbps — · ETA --:-- · 文件 0/0 · 已传 0 B")
        self.metrics.setObjectName("mutedLabel")
        progress_layout.addWidget(self.album_label)
        progress_layout.addWidget(self.progress)
        progress_layout.addWidget(self.metrics)
        layout.addWidget(progress_box)

        split = QHBoxLayout()
        album_box = QGroupBox("Album 文件")
        album_layout = QVBoxLayout(album_box)
        self.album_files = QListWidget()
        album_layout.addWidget(self.album_files)
        log_box = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        log_layout.addWidget(self.log)
        split.addWidget(album_box, 1)
        split.addWidget(log_box, 2)
        layout.addLayout(split, 1)

        bottom = QHBoxLayout()
        self.stop_button = QPushButton("安全停止")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_requested)
        bottom.addStretch(1)
        bottom.addWidget(self.stop_button)
        layout.addLayout(bottom)

    def start_session(self, kind: str, result: dict):
        self.title.setText(f"任务中心 · {'视频' if kind == 'video' else '图片'}")
        self.task_status.setText("正在启动…")
        self.stop_button.setEnabled(True)
        self.progress.setValue(0)
        self.metrics.setText(
            f"待上传 {result['pending_files']} 个 · {_fmt_size(result['pending_bytes'])} · "
            f"{result['album_count']} 个 Album"
        )
        self.album_files.clear()
        self.log.clear()

    @Slot(str, str)
    def add_message(self, level: str, text: str):
        prefix = {"success": "✓", "warning": "!", "error": "✗", "info": "ℹ"}.get(level, "·")
        self.log.appendPlainText(f"{prefix} {text}")
        self.task_status.setText(text.splitlines()[0][:100] if text else "运行中")

    @Slot(object)
    def show_album(self, payload: dict):
        self.album_label.setText(f"{payload.get('title', '')} · {payload.get('subtitle', '')}")
        self.album_files.clear()
        for row in payload.get("rows", []):
            self.album_files.addItem(str(row))

    @Slot(object)
    def show_progress(self, payload: dict):
        ratio = max(0.0, min(float(payload.get("ratio", 0)), 1.0))
        self.progress.setValue(round(ratio * 1000))
        speed = float(payload.get("speed", 0) or 0)
        mbps = speed * 8 / 1_000_000
        self.metrics.setText(
            f"速度 {_fmt_size(speed)}/s · {mbps:,.1f} Mbps · "
            f"ETA {_fmt_eta(payload.get('eta'))} · "
            f"Album {payload.get('album_number', 0)}/{payload.get('album_total', 0)} · "
            f"文件 {payload.get('done_files', 0)}/{payload.get('total_files', 0)} · "
            f"已传 {_fmt_size(payload.get('done_bytes', 0))} / {_fmt_size(payload.get('total_bytes', 0))}"
        )

    def finish_session(self, success: bool, message: str):
        self.stop_button.setEnabled(False)
        self.task_status.setText("已完成" if success else message)
        self.log.appendPlainText(("✓ " if success else "! ") + message)


class HistoryPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        title = QLabel("历史记录")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["时间", "类型", "来源", "文件", "数据量", "结果", "说明"])
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

    def reload_records(self):
        records = list(reversed(_load_history()))
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            values = [
                record.get("finished_at", record.get("started_at", "")),
                "视频" if record.get("kind") == "video" else "图片",
                record.get("source_dir", ""),
                str(record.get("total_files", 0)),
                _fmt_size(record.get("total_bytes", 0)),
                "成功" if record.get("success") else "停止/失败",
                record.get("message", ""),
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.resizeColumnsToContents()


class SettingsPage(QWidget):
    open_editor = Signal()
    clear_all_requested = Signal()
    clear_thumb_requested = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        title = QLabel("设置与诊断")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        self.config_status = QLabel()
        self.config_status.setWordWrap(True)
        layout.addWidget(self.config_status)

        env_box = QGroupBox("运行环境")
        env_layout = QFormLayout(env_box)
        self.env_labels = {}
        for name in ("PySide6", "tdjson", "Pillow", "imageio-ffmpeg", "ExifTool"):
            label = QLabel("检测中")
            self.env_labels[name] = label
            env_layout.addRow(name, label)
        self.env_labels["代理"] = QLabel("检测中")
        env_layout.addRow("代理", self.env_labels["代理"])
        layout.addWidget(env_box)

        config_box = QGroupBox("配置入口")
        config_layout = QHBoxLayout(config_box)
        edit = QPushButton("编辑配置")
        edit.setObjectName("primaryButton")
        edit.clicked.connect(self.open_editor)
        config_layout.addWidget(edit)
        config_layout.addWidget(QLabel("GUI 会保留现有 config.toml 注释；视频和图片目标可分别设置。"))
        config_layout.addStretch(1)
        layout.addWidget(config_box)

        license_box = QGroupBox("许可与署名")
        license_layout = QVBoxLayout(license_box)
        license_hint = QLabel(
            "原创内容采用 CC BY-NC 4.0（非商业）许可；TDLib、Qt/PySide6、"
            "Pillow、FFmpeg、PyInstaller 和 Python 仍按各自上游许可证使用。"
            "完整许可清单随程序放在 THIRD_PARTY_LICENSES.md。"
        )
        license_hint.setObjectName("mutedLabel")
        license_hint.setWordWrap(True)
        license_layout.addWidget(license_hint)
        layout.addWidget(license_box)

        cache_box = QGroupBox("缓存管理")
        cache_layout = QVBoxLayout(cache_box)
        self.cache_status = QLabel()
        self.cache_status.setObjectName("mutedLabel")
        self.cache_status.setWordWrap(True)
        cache_layout.addWidget(self.cache_status)
        cache_hint = QLabel(
            "清理所有会清空上传状态、旧版状态、视频封面和 GUI 历史记录；"
            "不会删除 config.toml 或 Telegram 登录数据库。"
        )
        cache_hint.setObjectName("mutedLabel")
        cache_hint.setWordWrap(True)
        cache_layout.addWidget(cache_hint)
        cache_buttons = QHBoxLayout()
        clear_thumb = QPushButton("仅清理视频封面 .thumb_cache")
        clear_thumb.setObjectName("secondaryButton")
        clear_thumb.clicked.connect(lambda: self.clear_thumb_requested.emit())
        clear_all = QPushButton("清理所有缓存")
        clear_all.setObjectName("dangerButton")
        clear_all.clicked.connect(lambda: self.clear_all_requested.emit())
        cache_buttons.addWidget(clear_thumb)
        cache_buttons.addWidget(clear_all)
        cache_buttons.addStretch(1)
        cache_layout.addLayout(cache_buttons)
        layout.addWidget(cache_box)
        layout.addStretch(1)
        self.refresh()

    def refresh(self):
        if _CONFIG_ERROR:
            self.config_status.setText(f"配置不可用：{_CONFIG_ERROR}")
            self.config_status.setStyleSheet("color: #ff7b72")
        elif _cfg("API_ID", 12345678) == 12345678 or _cfg("API_HASH", "YOUR_API_HASH") == "YOUR_API_HASH":
            self.config_status.setText("配置文件已找到，但 Telegram API 信息仍是示例值，请先编辑配置。")
            self.config_status.setStyleSheet("color: #f2cc60")
        else:
            self.config_status.setText(f"配置文件：{CONFIG_PATH}")
            self.config_status.setStyleSheet("color: #7ee787")

        exiftool_path = _cfg("EXIFTOOL_PATH", None)
        checks = {
            "PySide6": True,
            "tdjson": bool(importlib.util.find_spec("tdjson")),
            "Pillow": bool(importlib.util.find_spec("PIL")),
            "imageio-ffmpeg": bool(importlib.util.find_spec("imageio_ffmpeg")),
            "ExifTool": bool(cfg is not None and exiftool_path and Path(exiftool_path).exists()),
        }
        for name, available in checks.items():
            label = self.env_labels[name]
            label.setText("可用" if available else "未找到 / 可选")
            label.setStyleSheet(f"color: {'#7ee787' if available else '#f2cc60'}")
        proxy_label = self.env_labels["代理"]
        if _cfg("PROXY_ENABLED", False):
            proxy_type = {
                "socks5": "SOCKS5",
                "http": "HTTP",
                "mtproto": "MTProto",
            }.get(str(_cfg("PROXY_TYPE", "socks5")).lower(), "代理")
            proxy_label.setText(
                f"已启用 · {proxy_type} "
                f"{_cfg('PROXY_SERVER', '')}:{_cfg('PROXY_PORT', '')}"
            )
            proxy_label.setStyleSheet("color: #7ee787")
        else:
            proxy_label.setText("未启用 · 直连")
            proxy_label.setStyleSheet("color: #91a2b5")
        self.cache_status.setText(_cache_status_text())


class TargetDialog(QDialog):
    """Edit the Telegram target used by the selected media uploader."""

    def __init__(self, kind="video", parent=None):
        # Keep the old TargetDialog(parent) call shape usable for extensions.
        if not isinstance(kind, str):
            parent = kind if parent is None else parent
            kind = "video"
        super().__init__(parent)
        self.kind = kind if kind in {"video", "image"} else "video"
        accent = "视频" if self.kind == "video" else "图片"
        self.setWindowTitle(f"编辑{accent}上传目标与配置 · V1.8.2")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.form = form
        self.target_mode = QComboBox()
        self.target_mode.addItem("超级群组 Forum Topic", "forum_topic")
        self.target_mode.addItem("Channel 频道", "channel")
        form.addRow("目标类型", self.target_mode)
        self.chat_id = QLineEdit()
        self.channel_chat_id = QLineEdit()
        self.topic_id = QLineEdit()
        form.addRow("群组 Chat ID", self.chat_id)
        form.addRow("频道 Chat ID", self.channel_chat_id)
        form.addRow("Forum Topic ID", self.topic_id)
        layout.addLayout(form)

        self.media_box = QGroupBox(f"{accent}上传配置")
        self.media_form = QFormLayout(self.media_box)
        self._build_media_fields()
        layout.addWidget(self.media_box)

        hint = QLabel(
            "超级群组和频道的 Chat ID 通常以 -100 开头；频道不使用 Forum Topic。"
            "本窗口只显示当前上传类型的 Album、Caption 和处理选项。"
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.target_mode.currentIndexChanged.connect(self._update_fields)
        self._load_target()
        self._update_fields()

    def _build_media_fields(self):
        if self.kind == "video":
            self.video_missing_date = QComboBox()
            self.video_missing_date.addItems(["mtime", "error"])
            self.video_missing_date.setCurrentText(_cfg("VIDEO_MISSING_DATE_POLICY", "mtime"))
            self.media_form.addRow("日期缺失策略", self.video_missing_date)

            self.video_album = QSpinBox()
            self.video_album.setRange(1, 10)
            self.video_album.setValue(int(_cfg("VIDEO_ALBUM_SIZE", 10)))
            self.media_form.addRow("视频 Album 大小（普通模式）", self.video_album)

            self.video_force_ten = QCheckBox(
                "强制每 10 个视频组成一个 Album（忽略日期，默认关闭）"
            )
            self.video_force_ten.setChecked(
                bool(_cfg("VIDEO_FORCE_TEN_PER_ALBUM", False))
            )
            self.video_force_ten.toggled.connect(
                lambda enabled: self.video_album.setEnabled(not enabled)
            )
            self.video_force_ten.setToolTip(
                "开启后按扫描顺序连续分组，普通模式的 Album 大小和日期分组不参与。"
                "最后不足 10 个视频的一组会照常发送。"
            )
            self.media_form.addRow("视频分组", self.video_force_ten)
            self.video_album.setEnabled(not self.video_force_ten.isChecked())

            self.video_separator = QLineEdit(str(_cfg("VIDEO_ALBUM_CAPTION_SEPARATOR", " · ")))
            self.media_form.addRow("视频标题分隔符", self.video_separator)

            self.video_filenames = QCheckBox("视频标题附加“序号. 文件名”清单")
            self.video_filenames.setChecked(bool(_cfg("VIDEO_CAPTION_INCLUDE_FILENAMES", False)))
            self.media_form.addRow("视频描述", self.video_filenames)

            self.thumbnail = QCheckBox("生成视频缩略图")
            self.thumbnail.setChecked(bool(_cfg("VIDEO_GENERATE_THUMBNAIL", True)))
            self.media_form.addRow("视频处理", self.thumbnail)
        else:
            self.image_sort = QComboBox()
            self.image_sort.addItems(["mtime", "path"])
            self.image_sort.setCurrentText(_cfg("IMAGE_SORT_MODE", "mtime"))
            self.media_form.addRow("图片排序", self.image_sort)

            self.image_album = QSpinBox()
            self.image_album.setRange(1, 10)
            self.image_album.setValue(int(_cfg("IMAGE_ALBUM_SIZE", 10)))
            self.media_form.addRow("图片 Album 大小", self.image_album)

            self.image_numbering = QCheckBox("图片 Album 默认添加编号")
            self.image_numbering.setChecked(bool(_cfg("IMAGE_ALBUM_NUMBERING", True)))
            self.media_form.addRow("图片 Caption", self.image_numbering)

            self.image_separator = QLineEdit(str(_cfg("IMAGE_ALBUM_CAPTION_SEPARATOR", " · ")))
            self.media_form.addRow("图片标题分隔符", self.image_separator)

            self.image_filenames = QCheckBox("图片标题附加“序号. 文件名”清单")
            self.image_filenames.setChecked(bool(_cfg("IMAGE_CAPTION_INCLUDE_FILENAMES", False)))
            self.media_form.addRow("图片描述", self.image_filenames)

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
            values.update({
                ("video", "missing_date_policy"): self.video_missing_date.currentText(),
                ("video", "album_size"): self.video_album.value(),
                ("video", "force_ten_per_album"): self.video_force_ten.isChecked(),
                ("video", "album_caption_separator"): self.video_separator.text(),
                ("video", "caption_include_filenames"): self.video_filenames.isChecked(),
                ("video", "generate_thumbnail"): self.thumbnail.isChecked(),
            })
        else:
            values.update({
                ("image", "sort_mode"): self.image_sort.currentText(),
                ("image", "album_size"): self.image_album.value(),
                ("image", "album_numbering"): self.image_numbering.isChecked(),
                ("image", "album_caption_separator"): self.image_separator.text(),
                ("image", "caption_include_filenames"): self.image_filenames.isChecked(),
            })
        error = _write_config_values(values)
        if error:
            QMessageBox.critical(self, "保存失败", error)
            return
        self.accept()


class ConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑配置 · V1.8.2")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
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
        form.addRow("ExifTool 路径", field("exiftool_path", _cfg("EXIFTOOL_PATH", "tools/exiftool.exe")))
        layout.addLayout(form)

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
        layout.addWidget(proxy_box)

        hint = QLabel(
            "视频/图片的 Album、Caption 和处理选项请在各自上传页面的“编辑目标”中设置。"
            "API Hash、代理认证信息和 MTProto Secret 只写入本地 config.toml，不会写入 GUI 日志。"
            "代理由 TDLib 原生支持；tdjson 版本仍由项目固定要求控制。"
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
        def integer(name: str, fallback: int) -> int:
            try:
                return int(self.fields[name].text().strip())
            except ValueError:
                return fallback

        values = {
            ("telegram", "api_id"): integer("api_id", 12345678),
            ("telegram", "api_hash"): self.fields["api_hash"].text().strip(),
            ("paths", "video_dir"): self.fields["video_dir"].text().strip(),
            ("paths", "image_dir"): self.fields["image_dir"].text().strip(),
            ("paths", "exiftool_path"): self.fields["exiftool_path"].text().strip(),
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

    def _build_ui(self):
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(215)
        for label in ("概览", "视频上传", "图片上传", "任务中心", "历史记录", "设置与诊断"):
            self.sidebar.addItem(QListWidgetItem(label))
        root.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.home = HomePage()
        self.video_page = UploadPage("video")
        self.image_page = UploadPage("image")
        self.task_page = TaskPage()
        self.history_page = HistoryPage()
        self.settings_page = SettingsPage()
        for page in (self.home, self.video_page, self.image_page, self.task_page, self.history_page, self.settings_page):
            if isinstance(page, UploadPage):
                # Upload pages contain several stacked sections.  Keeping
                # them in a scroll area prevents the Telegram target and
                # action buttons from being clipped when the window is made
                # shorter or narrower.
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QFrame.Shape.NoFrame)
                scroll.setWidget(page)
                self.stack.addWidget(scroll)
            else:
                self.stack.addWidget(page)
        root.addWidget(self.stack, 1)

        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.sidebar.setCurrentRow(0)
        self.home.start_upload.connect(self._open_upload)
        self.home.open_settings.connect(lambda: self.sidebar.setCurrentRow(5))
        self.video_page.scan_requested.connect(self._scan)
        self.image_page.scan_requested.connect(self._scan)
        self.video_page.start_requested.connect(self._start_upload)
        self.image_page.start_requested.connect(self._start_upload)
        self.video_page.path_selected.connect(self._save_source_path)
        self.image_page.path_selected.connect(self._save_source_path)
        self.video_page.edit_target_requested.connect(self._edit_target)
        self.image_page.edit_target_requested.connect(self._edit_target)
        self.task_page.stop_requested.connect(self._stop_upload)
        self.settings_page.open_editor.connect(self._edit_config)
        self.settings_page.clear_all_requested.connect(self._clear_all_cache)
        self.settings_page.clear_thumb_requested.connect(self._clear_thumb_cache)

        self.statusBar().showMessage("就绪")

    def _refresh_pages(self):
        self.video_page.refresh_config()
        self.image_page.refresh_config()
        self.settings_page.refresh()
        self.history_page.reload_records()
        if _CONFIG_CREATED:
            self.statusBar().showMessage("已创建 config.toml，请先在设置中填写 Telegram 信息")

    def _open_upload(self, kind: str):
        self.sidebar.setCurrentRow(1 if kind == "video" else 2)
        self._scan(kind)

    def _scan(self, kind: str):
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
        page = self.video_page if kind == "video" else self.image_page
        page.set_scanning(True)
        worker.completed.connect(lambda result, k=kind: self._scan_done(k, result))
        worker.failed.connect(lambda message, k=kind: self._scan_failed(k, message))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _scan_done(self, kind: str, result: dict):
        self.scanners.pop(kind, None)
        page = self.video_page if kind == "video" else self.image_page
        page.set_scanning(False)
        page.set_result(result)
        self.home.update_scan(result)
        self.statusBar().showMessage(f"{kind} 扫描完成")

    def _scan_failed(self, kind: str, message: str):
        self.scanners.pop(kind, None)
        page = self.video_page if kind == "video" else self.image_page
        page.set_scanning(False)
        page.status_label.setText(message)
        self.statusBar().showMessage(message)

    def _save_source_path(self, kind: str, path: str):
        section_key = ("paths", "video_dir" if kind == "video" else "image_dir")
        answer = QMessageBox.question(
            self,
            "保存目录",
            f"是否将此目录保存为默认{('视频' if kind == 'video' else '图片')}目录？\n\n{path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            (self.video_page if kind == "video" else self.image_page).refresh_config()
            return
        error = _write_config_values({section_key: path})
        if error:
            QMessageBox.critical(self, "保存失败", error)
            (self.video_page if kind == "video" else self.image_page).refresh_config()
        else:
            self._refresh_pages()
            self.statusBar().showMessage("目录配置已保存")

    def _start_upload(self, kind: str):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "任务运行中", "图片和视频任务不能同时运行。")
            return
        page = self.video_page if kind == "video" else self.image_page
        result = page.result
        if not result or not result.get("pending_files"):
            QMessageBox.information(self, "无需上传", "当前没有待上传项目。")
            return
        if not result.get("core_available"):
            QMessageBox.warning(self, "依赖不完整", "当前环境只能预览，无法启动 TDLib 上传。请先运行 setup.ps1。")
            return
        if _cfg("API_ID", 12345678) == 12345678 or _cfg("API_HASH", "YOUR_API_HASH") == "YOUR_API_HASH":
            QMessageBox.warning(self, "尚未配置", "请先在设置中填写 Telegram API ID 和 API Hash。")
            self.sidebar.setCurrentRow(5)
            return

        answer = QMessageBox.question(
            self,
            "确认开始上传",
            f"待上传 {result['pending_files']} 个文件，约 {_fmt_size(result['pending_bytes'])}，共 {result['album_count']} 个 Album。\n\n确认开始？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.active_kind = kind
        self.active_result = result
        self.started_at = _dt.datetime.now().isoformat(timespec="seconds")
        self.task_page.start_session(kind, result)
        self.sidebar.setCurrentRow(3)
        worker = UploadWorker(kind, self.auth_bridge)
        self.worker = worker
        worker.ui.message_added.connect(self.task_page.add_message)
        worker.ui.progress_changed.connect(self.task_page.show_progress)
        worker.ui.album_changed.connect(self.task_page.show_album)
        worker.ui.target_changed.connect(self._target_from_worker)
        worker.completed.connect(self._upload_finished)
        worker.finished.connect(lambda w=worker: self._worker_thread_finished(w))
        page.set_running(True)
        self.home.task_value.setText(f"{'视频' if kind == 'video' else '图片'}上传中")
        self.home.set_connection("上传中", True)
        self.statusBar().showMessage("上传任务已启动")
        worker.start()

    def _target_from_worker(self, payload: dict):
        self.home.set_connection("已连接", True)
        kind = payload.get("kind") or self.active_kind
        page = self.video_page if kind == "video" else self.image_page
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
        page = self.video_page if self.active_kind == "video" else self.image_page
        page.set_running(False)
        self.home.task_value.setText("无")
        self.home.set_connection("已连接" if success else "未连接", success)
        self.statusBar().showMessage(message)
        self.history_page.reload_records()

    def _worker_thread_finished(self, worker: UploadWorker):
        if self.worker is worker:
            self.worker = None
        worker.deleteLater()

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
            self.video_page.clear_scan_result()
            self.image_page.clear_scan_result()
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
            "将清空视频/图片上传状态、旧版状态、视频封面缓存和 GUI 历史记录，保留目录本身。\n\n"
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
            "只清空 .thumb_cache 中的视频封面文件，保留目录本身，不影响上传状态和历史记录。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._finish_cache_clear(("thumb_cache",), reset_scan=False)

    def _edit_target(self, kind: str = "video"):
        dialog = TargetDialog(kind, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh_pages()
            saved_kind = dialog.kind
            self.statusBar().showMessage(
                f"{'视频' if saved_kind == 'video' else '图片'}上传目标已保存"
            )

    def _edit_config(self):
        dialog = ConfigDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._refresh_pages()
            self.statusBar().showMessage("配置已保存")

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
    app = QApplication(sys.argv)
    app.setApplicationName("TDLib Media Uploader")
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(QIcon(str(ICON_PATH)) if ICON_PATH.is_file() else QIcon())
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
