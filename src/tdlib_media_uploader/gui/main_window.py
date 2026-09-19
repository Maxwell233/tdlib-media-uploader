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
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
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
    UploadHubPage,
    UploadPage as _PackageUploadPage,
    UploadPageServices,
    VideoPage as _PackageVideoPage,
)
from .workers import ScanWorker, UploadWorker


PROJECT_DIR = RESOURCE_DIR
APP_VERSION = read_version()

from .tools import (
    MEDIA_KINDS,
    KIND_LABELS,
    CAPTION_EDITOR_SOFT_LIMIT,
    KIND_PATH_KEYS,
    KIND_PATH_CONFIG_KEYS,
    ICON_NAME,
    ICON_PATH,
    WINDOWS_APP_USER_MODEL_ID,
    require_kind as _require_kind,
    kind_label as _kind_label,
    format_size as _fmt_size,
    format_eta as _fmt_eta,
    format_date as _fmt_date,
    path_text as _path_text,
    hidden_subprocess_kwargs as _hidden_subprocess_kwargs,
    validate_exiftool_path,
    prepare_windows_app_identity as _prepare_windows_app_identity,
    prepare_qt_plugins as _prepare_qt_plugins,
    application_icon as _application_icon,
)
from .config_service import (
    ensure_config_file as _ensure_config_file_service,
    _CONFIG_CREATED,
    cfg,
    reload_config as _reload_config,
    get_cfg as _cfg,
    target_for as _target_for,
    source_root_for as _source_root_for,
    get_section_re as _get_section_re,
    get_key_re as _get_key_re,
    update_toml_value as _update_toml_value,
    write_config_values,
)
from .history_service import (
    HISTORY_PATH,
    load_history as _load_history,
    save_history as _save_history,
    clear_history as _clear_history,
)
from .cache_service import (
    CACHE_TARGETS,
    ALL_CACHE_KEYS,
    current_cache_targets,
    cache_usage as _cache_usage,
    cache_status_text as _cache_status_text,
    remove_cache_path as _remove_cache_path,
    clear_cache,
)
from .scanner import (
    BASIC_SCAN_SNAPSHOTS,
    basic_paths as _basic_paths,
    path_size as _path_size,
    item_size as _item_size,
    apply_size_limits as _apply_size_limits,
    basic_mixed_scan as _basic_mixed_scan,
    cancelled_scan_result as _cancelled_scan_result,
    legacy_scan_result as _legacy_scan_result,
    load_v2_gui_integration as _load_v2_gui_integration,
    v2_scan_result as _v2_scan_result,
    scan_result as _scan_result,
)


_validate_exiftool_path = validate_exiftool_path
_write_config_values = write_config_values
_current_cache_targets = current_cache_targets
_clear_cache = clear_cache


def _ensure_config_file() -> bool:
    """Ensure the user config.toml exists on startup, copying the bundled template."""
    if "--self-test" in sys.argv[1:]:
        return False
    return _ensure_config_file_service()


_prepare_qt_plugins()

from .dialogs import ConfigDialog, ScanToolsDialog, TargetDialog
from .pages import (
    HistoryPage,
    InflightPage,
    SettingsPage,
)
from .components import NavigationSidebar
from .theme import APP_STYLE, THEME


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
        self.upload_hub = UploadHubPage(self.video_page, self.image_page, self.mixed_page)
        self.task_page = TaskPage()
        self.inflight_page = InflightPage()
        self.history_page = HistoryPage()
        self.settings_page = SettingsPage()
        self.upload_pages = {
            "video": self.video_page,
            "image": self.image_page,
            "mixed": self.mixed_page,
        }
        self.sidebar_rows = {
            "dashboard": 0,
            "upload": 1,
            "video": 1,
            "image": 1,
            "mixed": 1,
            "task": 2,
            "inflight": 3,
            "history": 4,
            "settings": 5,
        }
        for page in (self.home, self.upload_hub, self.task_page, self.inflight_page, self.history_page, self.settings_page):
            if isinstance(page, SettingsPage):
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QFrame.Shape.NoFrame)
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
            page.edit_parameters_requested.connect(self._open_upload_parameters)
        self.task_page.safe_stop_requested.connect(self._safe_stop_upload)
        self.task_page.immediate_stop_requested.connect(self._immediate_stop_upload)
        self.inflight_page.reconciliation_requested.connect(self._reconcile_inflight)
        self.settings_page.open_editor.connect(self._edit_config)
        self.settings_page.open_scan_tools.connect(self._edit_scan_tools)
        self.settings_page.clear_all_requested.connect(self._clear_all_cache)
        self.settings_page.clear_thumb_requested.connect(self._clear_thumb_cache)
        self.settings_page.config_saved.connect(self._on_settings_saved)

        self.statusBar().showMessage("就绪")
        self._telegram_connected = False

    def refresh_theme(self):
        """Re-apply styling across composite widgets on theme switch."""
        targets = [
            getattr(self, "nav_sidebar", None),
            getattr(self, "home", None),
            getattr(self, "upload_hub", None),
            getattr(self, "video_page", None),
            getattr(self, "image_page", None),
            getattr(self, "mixed_page", None),
            getattr(self, "task_page", None),
            getattr(self, "inflight_page", None),
            getattr(self, "history_page", None),
            getattr(self, "settings_page", None),
        ]
        for target in targets:
            if target is not None and hasattr(target, "refresh_theme"):
                target.refresh_theme()

    def _refresh_pages(self):
        self.video_page.refresh_config()
        self.image_page.refresh_config()
        self.mixed_page.refresh_config()
        self.settings_page.refresh()
        self.inflight_page.reload_records()
        self.history_page.reload_records()
        if _CONFIG_CREATED:
            self.statusBar().showMessage("已创建 config.toml，请先在设置中填写 Telegram 信息")

    def _on_settings_saved(self):
        self._invalidate_previews()
        self._refresh_pages()
        self.statusBar().showMessage("设置已保存并生效")

    def _open_upload(self, kind: str):
        self.sidebar.setCurrentRow(self._sidebar_row(kind))
        if hasattr(self, "upload_hub"):
            self.upload_hub.set_current_kind(kind)
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
        api_hash = str(_cfg("API_HASH", "")).strip()
        if (
            _cfg("API_ID", 12345678) == 12345678
            or not api_hash
            or api_hash == "YOUR_API_HASH"
        ):
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
        worker.ui.progress_changed.connect(self._on_worker_progress)
        worker.ui.album_changed.connect(self.task_page.show_album)
        worker.ui.target_changed.connect(self._target_from_worker)
        worker.completed.connect(self._upload_finished)
        worker.finished.connect(lambda w=worker: self._worker_thread_finished(w))
        for upload_page in self.upload_pages.values():
            upload_page.set_running(True)
        summary = f"待上传 {result['pending_files']} 个文件 · {result.get('album_count', 0)} 个 Album"
        self.home.set_task_running(kind, summary)
        self.home.set_connection("上传中", True)
        self.nav_sidebar.set_connection_status(True, "上传中")
        self.statusBar().showMessage("上传任务已启动")
        worker.start()

    def _on_worker_progress(self, payload: dict):
        self.task_page.show_progress(payload)
        current = int(payload.get("done_files", 0) or 0)
        total = int(payload.get("total_files", 0) or 0)
        speed_val = float(payload.get("speed", 0) or 0)
        speed_str = f"{_fmt_size(speed_val)}/s" if speed_val > 0 else ""
        eta_val = payload.get("eta")
        eta_str = _fmt_eta(eta_val) if eta_val is not None else ""
        self.home.set_task_progress(current, total, speed_str, eta_str)

    def _target_from_worker(self, payload: dict):
        self._telegram_connected = True
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
        if self.active_kind:
            page = self._upload_page(self.active_kind)
            page.clear_scan_result()
        for upload_page in self.upload_pages.values():
            upload_page.set_running(False)
        self.home.set_task_idle()
        self.home.set_connection("已连接" if self._telegram_connected else "未连接", self._telegram_connected)
        self.nav_sidebar.set_connection_status(self._telegram_connected, "已连接" if self._telegram_connected else "未连接")
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

    def _open_upload_parameters(self, kind: str = "video"):
        kind = _require_kind(kind)
        if not self._can_change_configuration():
            return
        self.sidebar.setCurrentRow(self.sidebar_rows["settings"])
        self.settings_page.open_upload_parameters(kind)

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
        for scanner in list(self.scanners.values()):
            if scanner.isRunning():
                if hasattr(scanner, "request_stop"):
                    scanner.request_stop()
                elif hasattr(scanner, "cancel"):
                    scanner.cancel()
                if not scanner.wait(5000):
                    QMessageBox.warning(self, "仍在扫描", "扫描任务尚未结束，请稍候再关闭窗口。")
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
