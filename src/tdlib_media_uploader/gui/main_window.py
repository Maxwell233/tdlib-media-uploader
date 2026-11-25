# -*- coding: utf-8 -*-
"""PySide6 desktop interface for TDLib Media Uploader.

The GUI is the only user-facing interface.  Upload cores remain the source of
truth for scanning, Album creation, TDLib requests and resumable state.
"""

from __future__ import annotations

from functools import partial
import os
import sys
import tomllib
from pathlib import Path


if __name__ == "__main__" and not getattr(sys, "frozen", False):
    print(
        "此应用仅支持从发布包运行，请从 GitHub Releases 下载对应平台的程序包。",
        file=sys.stderr,
    )
    raise SystemExit(2)

from ..core.album import album_key
from ..core.logging import write_app_log
from PySide6.QtCore import QTimer, Signal, Slot, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QWidget,
)
from ..config.paths import (
    APP_DATA_DIR, CONFIG_PATH, RESOURCE_DIR, TEMPLATE_CONFIG_PATH, DATA_DIR,
    read_version, ensure_data_dirs,
)
from .instance_lock import InstanceLock
from .application import run_self_test
from .events import AuthBridge
from .pages import (
    HomePage,
    ImagePage,
    MixedPage,
    TaskPage,
    UploadHubPage,
    UploadPage,
    VideoPage,
)
from .workers import ScanWorker, UploadWorker
from .queue_controller import QueueController
from ..config.snapshot import snapshot_config
from ..telegram.session import TDLibSessionManager
from ..upload.queue import ACTION_REQUIRED, ACTIVE, TaskStatus, UploadTask
from ..upload.queue_store import UploadQueue


PROJECT_DIR = RESOURCE_DIR
APP_VERSION = read_version()

from .tools import (
    MEDIA_KINDS,
    CAPTION_EDITOR_SOFT_LIMIT,
    KIND_PATH_KEYS,
    KIND_PATH_CONFIG_KEYS,
    require_kind as _require_kind,
    kind_label as _kind_label,
    format_size as _fmt_size,
    format_eta as _fmt_eta,
    validate_exiftool_path,
    prepare_windows_app_identity as _prepare_windows_app_identity,
    prepare_qt_plugins as _prepare_qt_plugins,
    application_icon as _application_icon,
)
from .config_service import (
    _CONFIG_CREATED,
    cfg,
    get_cfg as _cfg,
    target_for as _target_for,
    source_root_for as _source_root_for,
    update_toml_value as _update_toml_value,
    write_config_values,
)
from . import config_service
from .history_service import load_history as _load_history, save_history as _save_history
from .cache_service import ALL_CACHE_KEYS, clear_cache, current_cache_targets, cache_usage as _cache_usage
from .scanner import scan_result as _scan_result


_validate_exiftool_path = validate_exiftool_path
_write_config_values = write_config_values


_prepare_qt_plugins()

from .dialogs import TargetDialog
from .pages import (
    HistoryPage,
    InflightPage,
    SettingsPage,
)
from .components import NavigationSidebar
from .theme import initialize_theme
from .preferences import UiPreferences


class MainWindow(QMainWindow):
    telegram_connection_changed = Signal(str)

    def __init__(self, *, queue_store=None, preferences=None):
        super().__init__()
        global cfg
        cfg = config_service.get_config()
        application = QApplication.instance()
        if application is not None:
            self.setWindowIcon(application.windowIcon())
        self.setWindowTitle(f"TDLib Media Uploader · V{APP_VERSION} · Maximum 2026")
        self.setMinimumSize(860, 560)
        self.resize(1240, 800)
        self._cache_clear_worker = None
        self._close_after_recovery = False
        self._close_after_upload = False
        self._close_after_session = False
        self.scanners: dict[str, ScanWorker] = {}
        self._build_ui(preferences)
        self.telegram_connection_changed.connect(self._telegram_connection, Qt.ConnectionType.QueuedConnection)
        self.telegram_session = TDLibSessionManager(self.telegram_connection_changed.emit)
        store = queue_store if queue_store is not None else UploadQueue(DATA_DIR / "upload_queue.json")
        self.queue = QueueController(store, self._make_upload_worker, self._can_start_queue, self)
        self.queue.changed.connect(self._refresh_queue)
        self.queue.error.connect(self._queue_error)
        self.queue.launched.connect(self._upload_launched)
        self.queue.ended.connect(self._upload_finished)
        self.task_page.queue_pause_requested.connect(self.queue.request_paused)
        self.task_page.disconnect_requested.connect(self._disconnect_telegram)
        self.task_page.queue_move_requested.connect(self.queue.move)
        self.task_page.queue_top_requested.connect(self.queue.move_to_top)
        self.task_page.queue_remove_requested.connect(self.queue.remove)
        self.task_page.queue_restore_requested.connect(self._restore_queued_task)
        self.task_page.queue_recheck_requested.connect(self._recheck_queued_task)
        self.task_page.queue_skip_requested.connect(self._skip_queued_task)
        self.task_page.album_skip_requested.connect(self._skip_queued_album)
        self.task_page.album_retry_requested.connect(self._retry_queued_album)
        self.task_page.queue_inflight_requested.connect(
            lambda: self.sidebar.setCurrentRow(self.sidebar_rows["inflight"])
        )
        self.queue.skipped.connect(self._record_skipped_task)
        self.queue.recovery_progress.connect(self.task_page.show_recovery_progress)
        self.queue.recovery_finished.connect(self._queue_recovery_finished)
        self.task_page.queue_cancel_requested.connect(self.queue.cancel_recovery)
        self.settings_page.storage_panel.cancel_cleanup_requested.connect(self._cancel_cache_clear)
        self._refresh_pages()
        self._refresh_queue()
        if store.error:
            write_app_log("ERROR", store.error, source="upload/queue")
            self.statusBar().showMessage(store.error)
        elif store.paused and any(task.status in ACTION_REQUIRED | {TaskStatus.PENDING} for task in store.tasks):
            self.statusBar().showMessage("发现未完成的上传队列。请在任务中心查看并继续队列。")

    @property
    def worker(self):
        return self.queue.worker

    def _upload_page(self, kind: str):
        """Return a media page after validating the explicit route kind."""

        return self.upload_pages[_require_kind(kind)]

    def _sidebar_row(self, kind: str) -> int:
        return self.sidebar_rows[_require_kind(kind)]

    def _build_ui(self, preferences=None):
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        self.nav_sidebar = NavigationSidebar(version=APP_VERSION, preferences=preferences)
        self.nav_sidebar.theme_save_failed.connect(self._theme_warning)
        self.sidebar = self.nav_sidebar.list
        root.addWidget(self.nav_sidebar)

        self.stack = QStackedWidget()
        self.home = HomePage()
        self.video_page = VideoPage()
        self.image_page = ImagePage()
        self.mixed_page = MixedPage()
        self.upload_hub = UploadHubPage(self.video_page, self.image_page, self.mixed_page)
        self.task_page = TaskPage()
        self.inflight_page = InflightPage()
        self.history_page = HistoryPage()
        self.settings_page = SettingsPage(can_save=self._can_change_configuration)
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
            page.reset_state_requested.connect(self._reset_upload_state)
            page.start_requested.connect(self._start_upload)
            page.path_selected.connect(self._save_source_path)
            page.edit_target_requested.connect(self._edit_target)
            page.edit_parameters_requested.connect(self._open_upload_parameters)
        self.task_page.safe_stop_requested.connect(self._safe_stop_upload)
        self.task_page.immediate_stop_requested.connect(self._immediate_stop_upload)
        self.inflight_page.reconciliation_requested.connect(self._reconcile_inflight)
        self.settings_page.clear_all_requested.connect(self._clear_all_cache)
        self.settings_page.clear_thumb_requested.connect(self._clear_thumb_cache)
        self.settings_page.clear_video_processed_requested.connect(
            self._clear_video_processed_cache
        )
        self.settings_page.config_saved.connect(self._on_settings_saved)

        self.statusBar().showMessage("就绪")

    def _theme_warning(self, message):
        write_app_log("WARNING", message, source="ui/preferences")
        self.statusBar().showMessage(message)

    def refresh_theme(self):
        """Re-apply styling across composite widgets on theme switch."""
        for target in (
            self.nav_sidebar,
            self.home,
            self.upload_hub,
            self.task_page,
            self.inflight_page,
            self.history_page,
            self.settings_page,
        ):
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
        self.upload_hub.set_current_kind(kind)
        self._scan(kind)

    def _scan(self, kind: str):
        if self._cache_clear_worker is not None:
            self.statusBar().showMessage("请等待缓存清理结束，或先取消清理")
            return False
        kind = _require_kind(kind)
        if self.scanners:
            self.statusBar().showMessage("已有目录扫描在运行，请等待完成或取消")
            return False
        config = config_service.get_config()
        if config is None:
            self.statusBar().showMessage("配置不可用，请检查设置")
            return False
        target = _target_for(kind)
        root = _source_root_for(kind)
        frozen = snapshot_config(config, kind, target, immutable=True)
        runner = partial(_scan_result, config=frozen, target=target, source_root=root)
        worker = ScanWorker(kind, scan_runner=runner)
        self.scanners[kind] = worker
        self._upload_page(kind).set_scanning(True)
        worker.progress_changed.connect(
            lambda k, event, w=worker: self._scan_progress(k, event)
            if self.scanners.get(k) is w else None, Qt.ConnectionType.QueuedConnection,
        )
        worker.finished.connect(
            lambda w=worker: self._scan_finished(w), Qt.ConnectionType.QueuedConnection
        )
        worker.start()
        return True

    def _scan_finished(self, worker):
        kind = worker.kind
        if self.scanners.get(kind) is not worker:
            return
        if worker.result is not None:
            self._scan_done(kind, {**worker.result, "scan_id": worker.scan_id})
        else:
            self._scan_failed(kind, worker.error or "扫描线程没有返回结果")
        worker.deleteLater()


    @Slot(str)
    def _reset_upload_state(self, kind: str):
        kind = _require_kind(kind)
        if self.queue.recovery_busy:
            self.statusBar().showMessage("请等待当前队列检查结束后再重置断点")
            return
        if self._cache_clear_worker is not None:
            self.statusBar().showMessage("请等待缓存清理结束后再重置断点")
            return
        if (self.worker is not None and self.worker.isRunning()) or any(
            scanner.isRunning() for scanner in self.scanners.values()
        ):
            QMessageBox.warning(self, "任务运行中", "请等待扫描完成或安全停止上传后再重置断点。")
            return
        if self.queue.store.error or any(task.kind == kind and task.status in ACTIVE | ACTION_REQUIRED | {TaskStatus.PENDING}
               for task in self.queue.store.tasks):
            self.statusBar().showMessage("此媒体类型仍有队列任务，请先处理任务再重置断点")
            return
        label = _kind_label(kind)
        answer = QMessageBox.warning(
            self,
            f"确认重置{label}断点",
            f"将一次性清空{label}类型的全部本地断点（包括不同来源目录和上传目标的记录）。\n"
            "已上传文件下次扫描会重新列为待上传，继续上传可能产生重复消息。\n\n"
            "其他类型断点和未确认上传记录会保留；未确认上传仍需核对处理。是否重置？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes or self.queue.recovery_busy:
            return
        _removed, errors = clear_cache((f"{kind}_state",), targets=current_cache_targets())
        self._upload_page(kind).clear_scan_result()
        self.home.clear_scan()
        if errors:
            QMessageBox.warning(self, "断点重置未完成", "\n".join(errors))
            self.statusBar().showMessage(f"{label}断点重置未完成，请重新扫描")
            return
        self.statusBar().showMessage(f"{label}断点已重置，请重新扫描；未确认上传记录已保留")

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
            self._upload_page(kind).clear_scan_result()
            self._upload_page(kind).refresh_config()
            self.settings_page.refresh()
            self.statusBar().showMessage("目录配置已保存")

    def _refresh_queue(self):
        recovering = self.queue.recovery_busy
        self.task_page.show_queue(self.queue.store, self.queue.write_blocked, recovering=recovering)
        self.inflight_page.set_review_blocked(recovering)
        immediate = (
            self.worker is None and not recovering and not self.queue.store.paused
            and not self.queue.store.error and not self.queue.store.blocked_reason
        )
        for page in self.upload_pages.values():
            page.set_queue_immediate(immediate)
        self._update_disconnect_button()

    def _update_disconnect_button(self):
        self.task_page.disconnect_button.setEnabled(
            self.worker is None and self.telegram_session.connection_state not in {"disconnected", "closing"}
        )

    @Slot(str)
    def _telegram_connection(self, _state):
        state = self.telegram_session.connection_state
        label = {
            "disconnected": "未连接",
            "closing": "正在关闭 Telegram",
            "authorization_required": "需要登录 Telegram",
            "connectionStateReady": "已连接",
            "connectionStateWaitingForNetwork": "等待网络恢复",
            "connectionStateConnectingToProxy": "正在连接代理",
            "connectionStateUpdating": "正在同步 Telegram",
        }.get(state, "正在连接 Telegram")
        connected = state == "connectionStateReady"
        self.home.set_connection(label, connected)
        self.nav_sidebar.set_connection_status(connected, label)
        self._update_disconnect_button()
        if state == "disconnected" and self.statusBar().currentMessage() == "正在关闭 Telegram 会话":
            self.statusBar().showMessage("Telegram 会话已关闭")

    @Slot()
    def _disconnect_telegram(self):
        if self.worker is not None or not self.queue.set_paused(True):
            return
        try:
            self.telegram_session.disconnect()
        except TimeoutError:
            self.statusBar().showMessage("正在关闭 Telegram 会话")

    def _queue_recovery_allowed(self):
        if self.queue.recovery_busy:
            self.statusBar().showMessage("请等待当前队列检查结束")
            return False
        if self._cache_clear_worker is not None:
            self._queue_error("请等待缓存清理结束后再处理队列任务。")
            return False
        return True

    def _restore_queued_task(self, task_id):
        if self._queue_recovery_allowed():
            self.queue.restore(task_id)

    def _recheck_queued_task(self, task_id):
        if self._queue_recovery_allowed():
            self.queue.recheck(task_id)

    def _queue_recovery_finished(self, result):
        if result.cancelled:
            self.task_page.queue_notice.setText("已取消队列检查。")
            self.statusBar().showMessage("已取消队列检查。")
        elif result.error is None:
            message = {
                "restore": "任务已恢复到原位置。请选择继续队列。",
                "recheck": "记录已核对。请选择恢复或跳过此任务。",
                "skip": "已跳过未完成的上传任务。请选择继续队列。",
                "skip_album": "已跳过所选 Album。请继续队列。",
                "retry_album": "已验证所选 Album 的失败文件。继续队列后将重试这些文件。",
                "refresh_reviews": "未确认上传记录已更新。请在任务中心查看恢复状态。",
            }[result.action]
            self.task_page.queue_notice.setText(message)
            self.statusBar().showMessage(message)
            if result.task is not None and result.action in {"skip", "skip_album"}:
                self._upload_page(result.task.kind).sync_task_outcomes(result.task)
        if self._close_after_recovery:
            self._close_after_recovery = False
            QTimer.singleShot(0, self.close)

    def _skip_queued_task(self, task_id):
        if not self._queue_recovery_allowed():
            return
        task = next((task for task in self.queue.store.tasks if task.task_id == task_id), None)
        if task is None:
            return
        answer = QMessageBox.question(
            self, "跳过未完成任务", f"跳过 {task.source_root} 的任务？其余未上传文件将留在原目录。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes and self._queue_recovery_allowed():
            self.queue.skip(task_id)

    def _skip_queued_album(self, task_id, album_key):
        if not self._queue_recovery_allowed():
            return
        answer = QMessageBox.question(
            self, "跳过 Album", "是否跳过所选 Album 中仍未上传的文件？原始文件将保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes and self._queue_recovery_allowed():
            self.queue.skip_album(task_id, album_key)

    def _retry_queued_album(self, task_id, album_key):
        if self._queue_recovery_allowed():
            self.queue.retry_album(task_id, album_key)

    def _record_skipped_task(self, task):
        message = "已跳过未完成的上传任务。"
        self._record_task_history(task, message)

    def _record_task_history(self, task, message):
        records = _load_history()
        records.append({
            "task_id": task.task_id, "started_at": task.started_at, "finished_at": task.finished_at,
            "kind": task.kind, "source_dir": str(task.source_root), "total_files": len(task.items),
            "total_bytes": task.total_bytes, "success": task.status is TaskStatus.COMPLETED,
            "status": task.status.value, "message": message,
        })
        _save_history(records)
        self.history_page.reload_records()

    def _queue_error(self, message):
        write_app_log("ERROR", message, source="upload/queue")
        self.task_page.queue_notice.setText(message)
        self.statusBar().showMessage(message)

    def _start_upload(self, kind: str):
        if self.queue.recovery_busy:
            self.statusBar().showMessage("请等待当前队列检查结束后再入队")
            return
        page = self._upload_page(kind)
        result = page.result
        if not result or not result.get("pending_files"):
            self.statusBar().showMessage("请先扫描包含待上传媒体的目录")
            return
        try:
            task = UploadTask.from_preview(kind, result, config_service.get_config())
        except (ValueError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, "无法加入队列", str(exc))
            return

        target = task.target
        answer = QMessageBox.question(
            self,
            "确认加入上传队列",
            f"待上传 {result['pending_files']} 个文件，约 {_fmt_size(result['pending_bytes'])}，"
            f"共 {result['album_count']} 组。\n"
            f"来源：{task.source_root}\n目标 Chat ID：{target['chat_id']}\n"
            + (f"话题 ID：{target['forum_topic_id']}\n" if target["forum_topic_id"] else "目标类型：频道\n")
            + "入队后会按队列顺序自动执行。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if (answer == QMessageBox.StandardButton.Yes and not self.queue.recovery_busy
                and self.queue.enqueue(task)):
            self.statusBar().showMessage("上传任务已加入队列，可继续扫描")

    def _can_start_queue(self):
        if self._cache_clear_worker is not None:
            return False
        from ..telegram.tdlib_common import TDJsonClient

        api_hash = str(_cfg("API_HASH", "")).strip()
        if _cfg("API_ID", 12345678) in {0, 12345678} or api_hash in {"", "YOUR_API_HASH"}:
            self.queue.set_paused(True)
            self._queue_error("队列已暂停，请先配置 Telegram API ID 和 API Hash，再继续队列。")
            return False
        active_client = TDJsonClient.unclosed_instance()
        if active_client is not None and not self.telegram_session.owns(active_client):
            self.queue.set_paused(True)
            self._queue_error("队列已暂停，请等待 Telegram 数据库关闭后再继续。")
            return False
        return True

    def _make_upload_worker(self, task):
        auth_bridge = AuthBridge()
        worker = UploadWorker(
            task.kind, auth_bridge, task.preview(),
            target_provider=lambda _kind: task.target,
            config=task.execution_config(config_service.get_config()),
            session_manager=self.telegram_session,
            queue_task_id=task.task_id,
        )
        worker.wait_for_scheduler = True
        auth_bridge.requested.connect(
            lambda prompt, password, w=worker: self._show_auth_dialog(w, prompt, password),
            Qt.ConnectionType.QueuedConnection,
        )
        return worker

    def _upload_launched(self, task, worker):
        plans = task.execution_plans()
        pending = [item for plan in plans for item in plan.pending_items]
        self.task_page.select_task(task.task_id)
        self.task_page.start_session(task.kind, {
            "pending_files": len(pending),
            "pending_bytes": sum(item.snapshot.size for item in pending),
            "album_count": sum(bool(plan.pending_items) for plan in plans),
        })
        for signal, callback in (
            (worker.ui.message_added, self.task_page.add_message),
            (worker.ui.progress_changed, self._on_worker_progress),
            (worker.ui.rate_limit_changed, self.task_page.show_rate_limit),
            (worker.ui.album_changed, self.task_page.show_album),
            (worker.ui.album_attempt_changed, self.task_page.show_album_attempt),
            (worker.ui.preliminary_progress_changed, self.task_page.show_preliminary_progress),
            (worker.ui.target_changed, self._target_from_worker),
        ):
            signal.connect(
                lambda *args, w=worker, handler=callback: handler(*args) if self.worker is w else None,
                Qt.ConnectionType.QueuedConnection,
            )
        for page in self.upload_pages.values():
            page.set_running(True)
        self.home.set_task_running(task.kind, f"{len(pending)} 个待上传文件 · {len(plans)} 个 Album")
        self._update_disconnect_button()

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
        is_channel = payload.get("target_mode") == "channel"
        self.statusBar().showMessage(
            f"目标已确认：{payload.get('chat_title')}"
            + ("（频道）" if is_channel else f" / {payload.get('topic_name')}")
        )

    def _safe_stop_upload(self):
        worker = self.worker
        if worker is not None and worker.isRunning():
            answer = QMessageBox.question(
                self,
                "安全停止上传",
                "当前正在发送的 Album 会继续完成并保存断点；完成后不再开始新的 Album。"
                "是否安全停止？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes and self.worker is worker:
                worker.request_safe_stop()
                self.task_page.task_status.setText("当前 Album 完成后安全停止…")
                self.statusBar().showMessage("已请求安全停止，当前 Album 将继续完成…")

    def _immediate_stop_upload(self):
        worker = self.worker
        if worker is not None and worker.isRunning():
            answer = QMessageBox.warning(
                self,
                "立即中断上传",
                "将立即请求取消当前 TDLib 上传。当前 Album 可能已经部分或全部提交给 Telegram，"
                "结果可能进入“未确认上传”，需要人工核对后才能继续。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes and self.worker is worker:
                worker.request_immediate_stop()
                self.task_page.task_status.setText("正在立即中断…")
                self.statusBar().showMessage("正在立即中断上传任务；当前 Album 可能进入 UNKNOWN…")

    def _upload_finished(self, task):
        success = task.status is TaskStatus.COMPLETED
        if self.queue.write_blocked:
            message = "任务结果未写入队列，已停止调度。请检查数据目录后重启。"
        else:
            message = {
                TaskStatus.COMPLETED: "上传任务完成。",
                TaskStatus.NEEDS_REVIEW: "请在“未确认上传”核对发送记录。",
                TaskStatus.INTERRUPTED: "任务已停止。可在任务中心重新验证并恢复。",
                TaskStatus.FAILED: "任务部分完成。" if task.outcome == "PARTIAL" else "任务未完成。",
            }[task.status]
            if task.error:
                message += f" {task.error}"
            self._record_task_history(task, message)

        write_app_log("INFO" if success else "ERROR", message, source=f"upload/{task.kind}")
        self.task_page.finish_session(success, message.splitlines()[0][:180])
        for upload_page in self.upload_pages.values():
            upload_page.set_running(False)
        if not self.queue.write_blocked:
            self._upload_page(task.kind).sync_task_outcomes(task)
        self.home.set_task_idle()
        self._update_disconnect_button()
        self.statusBar().showMessage(message.splitlines()[0][:180])
        self.inflight_page.reload_records()
        if self._close_after_upload:
            self._close_after_upload = False
            QTimer.singleShot(0, self.close)

    @Slot(object, bool)
    def _reconcile_inflight(self, record: object, sent: bool):
        """Apply a confirmed manual decision without querying Telegram."""

        if not self._queue_recovery_allowed():
            return
        if not isinstance(record, dict):
            return
        if self.worker is not None or any(scanner.isRunning() for scanner in self.scanners.values()):
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
            from ..upload.reconciliation import ReconciliationService, SourceRootRequired

            client = ReconciliationService()
            target = record.get("target") if isinstance(record.get("target"), dict) else {
                key: record.get(key)
                for key in ("target_mode", "chat_id", "forum_topic_id", "channel_chat_id")
                if key in record
            }
            try:
                confirmation = client.reconcile_inflight(album_key, sent=bool(sent), kind=kind, target=target or None)
            except SourceRootRequired:
                source_root = QFileDialog.getExistingDirectory(
                    self, "旧记录缺少来源目录：请选择当时扫描的根目录")
                if not source_root or not self._queue_recovery_allowed():
                    return
                confirmation = client.reconcile_inflight(
                    album_key, sent=bool(sent), kind=kind, target=target or None, source_root=source_root,
                )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "人工处理未完成",
                f"{exc}\n\n记录仍会保留，以避免重复上传。",
            )
            return
        self.inflight_page.reload_records()
        self.queue.refresh_reviews(record)
        if confirmation is not None:
            self._upload_page(kind).sync_reconciled_album(confirmation)

    def _cache_operation_allowed(self) -> bool:
        if self.queue.recovery_busy:
            self.statusBar().showMessage("请等待当前队列检查结束后再清理缓存")
            return False
        from ..telegram.tdlib_common import TDJsonClient
        active_client = TDJsonClient.unclosed_instance()
        if active_client is not None and not self.telegram_session.owns(active_client):
            self.statusBar().showMessage("请等待 Telegram 任务和数据库关闭后再清理缓存")
            return False
        if self._cache_clear_worker is not None:
            self.statusBar().showMessage("请等待缓存清理结束，或先取消清理")
            return False
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "任务运行中", "上传任务运行时不能清理缓存，请先安全停止任务。")
            return False
        active_scans = [worker for worker in self.scanners.values() if worker.isRunning()]
        if active_scans:
            QMessageBox.warning(self, "扫描运行中", "目录扫描运行时不能清理缓存，请等待扫描完成。")
            return False
        return True

    def _finish_cache_clear(self, keys: tuple[str, ...], *, reset_scan: bool):
        if self.queue.recovery_busy:
            self.statusBar().showMessage("请等待当前队列检查结束后再清理缓存")
            return
        from .workers import CacheClearWorker
        worker = CacheClearWorker(keys, current_cache_targets(), self)
        self._cache_clear_worker = worker
        self._cache_reset_scan = reset_scan
        panel = self.settings_page.storage_panel
        panel.set_cleaning(True)
        worker.progress.connect(panel.cache_status.setText)
        worker.finished.connect(self._cache_clear_done)
        worker.start()

    def _cancel_cache_clear(self):
        worker = self._cache_clear_worker
        if worker is not None:
            worker.request_stop()

    def _cache_clear_done(self):
        worker = self._cache_clear_worker
        if worker is None:
            return
        removed, errors = worker.result
        cancelled = worker.cancel_event.is_set()
        reset_scan = self._cache_reset_scan
        self._cache_clear_worker = None
        worker.deleteLater()
        self.settings_page.storage_panel.set_cleaning(False)
        self.settings_page.refresh()
        self.queue.schedule()
        if reset_scan:
            for upload_page in self.upload_pages.values():
                upload_page.clear_scan_result()
            self.home.clear_scan()
            self.history_page.reload_records()
        if cancelled:
            self.statusBar().showMessage("清理已取消；已删除的缓存不会恢复")
            self.settings_page.storage_panel.cache_status.setText("清理已取消")
            return
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
        if self.queue.store.error or any(
            task.status in ACTIVE | ACTION_REQUIRED | {TaskStatus.PENDING} for task in self.queue.store.tasks
        ):
            self.statusBar().showMessage("请先处理未完成的上传任务，再清理断点和缓存")
            return
        answer = QMessageBox.warning(
            self,
            "确认清理所有缓存",
            "将清空视频/图片/混合上传断点、标题、媒体缓存、历史记录、暂存副本和运行日志。\n\n"
            "未确认上传记录、队列、配置和 Telegram 登录数据库将保留。是否继续？",
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

    def _clear_video_processed_cache(self):
        if not self._cache_operation_allowed():
            return
        answer = QMessageBox.question(
            self,
            "确认清理已处理视频缓存",
            "只清空临时重新封装的已处理视频缓存，不影响原文件、视频封面缓存、上传状态和历史记录。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._finish_cache_clear(("video_processed",), reset_scan=False)

    def _edit_target(self, kind: str = "video"):
        kind = _require_kind(kind)
        if not self._can_change_configuration():
            return
        dialog = TargetDialog(kind, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            saved_kind = dialog.kind
            self._upload_page(saved_kind).clear_scan_result()
            self._upload_page(saved_kind).refresh_config()
            self.settings_page.refresh()
            self.statusBar().showMessage(
                f"{_kind_label(saved_kind)}上传目标已保存"
            )

    def _open_upload_parameters(self, kind: str = "video"):
        kind = _require_kind(kind)
        if not self._can_change_configuration():
            return
        self.sidebar.setCurrentRow(self.sidebar_rows["settings"])
        self.settings_page.open_upload_parameters(kind)

    def _can_change_configuration(self):
        if self._cache_clear_worker is not None:
            self.statusBar().showMessage("请等待缓存清理结束，或先取消清理")
            return False
        if self.scanners:
            QMessageBox.information(self, "正在扫描", "请等待扫描完成或先取消扫描，再修改配置。")
            return False
        return True

    def _invalidate_previews(self):
        for upload_page in self.upload_pages.values():
            upload_page.clear_scan_result()
        self.home.clear_scan()

    def _show_auth_dialog(self, worker, prompt: str, password: bool):
        if self.worker is not worker or worker.ui.safe_stop_requested:
            return
        echo = QLineEdit.EchoMode.Password if password else QLineEdit.EchoMode.Normal
        value, accepted = QInputDialog.getText(self, "Telegram 登录", prompt, echo)
        if not password:
            value = value.strip()
        if accepted and value:
            worker.ui.auth_bridge.answer(value)
        else:
            worker.ui.auth_bridge.answer("")
            if self.worker is worker:
                worker.request_stop()

    def closeEvent(self, event):
        if self._close_after_upload:
            event.ignore()
            return
        if self.queue.recovery_busy:
            self._close_after_recovery = True
            self.queue.closing = True
            self.queue.cancel_recovery()
            self.statusBar().showMessage("正在等待队列检查安全结束后关闭窗口")
            event.ignore()
            return
        cleanup = self._cache_clear_worker
        if cleanup is not None and cleanup.isRunning():
            cleanup.request_stop()
            if not cleanup.wait(1000):
                self.statusBar().showMessage("正在停止缓存清理，请稍后再关闭窗口")
                event.ignore()
                return
        worker = self.worker
        self.queue.closing = True
        if worker is not None and worker.isRunning():
            answer = QMessageBox.question(
                self,
                "任务运行中",
                "上传任务仍在运行。是否等待当前 Album 完成后退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.queue.closing = False
                self.queue.schedule()
                event.ignore()
                return
            if self.worker is worker and worker.isRunning():
                self._close_after_upload = True
                self.queue.set_paused(True)
                worker.request_safe_stop()
                self.statusBar().showMessage("正在等待当前 Album 安全停止后退出")
                event.ignore()
                return
        self.queue.finish_after_wait()
        from ..telegram.tdlib_common import TDJsonClient
        closing_client = TDJsonClient.unclosed_instance()
        try:
            if closing_client is None or self.telegram_session.owns(closing_client):
                self.telegram_session.disconnect()
            else:
                closing_client.close(timeout=0)
        except TimeoutError:
            self.statusBar().showMessage("正在等待 Telegram 安全关闭数据库")
            if not self._close_after_session:
                self._close_after_session = True
                QTimer.singleShot(100, self._retry_close_session)
            event.ignore()
            return
        for scanner in list(self.scanners.values()):
            if scanner.isRunning():
                scanner.request_stop()
                if not scanner.wait(5000):
                    QMessageBox.warning(self, "仍在扫描", "扫描任务尚未结束，请稍候再关闭窗口。")
                    self.queue.closing = False
                    event.ignore()
                    return
        cache_worker = self.settings_page.storage_panel._cache_worker
        if cache_worker is not None and cache_worker.isRunning():
            cache_worker.request_stop()
            if not cache_worker.wait(1000):
                self.statusBar().showMessage("正在停止缓存统计，请稍后再关闭窗口")
                self.queue.closing = False
                event.ignore()
                return
        event.accept()

    def _retry_close_session(self):
        self._close_after_session = False
        self.close()


def main() -> int:
    global cfg, _CONFIG_CREATED
    if not getattr(sys, "frozen", False):
        print(
            "此应用仅支持从发布包运行，请从 GitHub Releases 下载对应平台的程序包。",
            file=sys.stderr,
        )
        return 2
    if "--self-test" in sys.argv[1:]:
        return run_self_test()
    _prepare_windows_app_identity()
    _prepare_qt_plugins()
    app = QApplication.instance() or QApplication(sys.argv)
    try:
        ensure_data_dirs()
    except OSError as exc:
        error = f"无法初始化数据目录：{DATA_DIR}\n{type(exc).__name__}: {exc}"
        print(error, file=sys.stderr)
        write_app_log("ERROR", error, source="startup")
        QMessageBox.critical(app.activeWindow(), "启动失败", error)
        return 1
    error = config_service.initialize_config()
    if error:
        print(error, file=sys.stderr)
        write_app_log("ERROR", error, source="startup")
        QMessageBox.critical(app.activeWindow(), "配置初始化失败", error)
        return 1
    cfg = config_service.get_config()
    _CONFIG_CREATED = config_service._CONFIG_CREATED
    instance_lock = InstanceLock(DATA_DIR / "app.lock")
    if not instance_lock.acquire():
        QMessageBox.warning(app.activeWindow(), "程序已在运行", "TDLib Media Uploader 已在运行。")
        app.quit()
        return 1
    try:
        write_app_log("INFO", f"启动 TDLib Media Uploader V{APP_VERSION}", source="startup")
        from ..media.artifact_cache import prune_media_artifacts
        prune_media_artifacts(cfg, startup=True)
        app.setApplicationName("TDLib Media Uploader")
        app.setApplicationVersion(APP_VERSION)
        app.setWindowIcon(_application_icon())
        app.setStyle("Fusion")
        preferences = UiPreferences()
        initialize_theme(app, preferences)
        window = MainWindow(preferences=preferences)
        if preferences.error:
            window._theme_warning(preferences.error)
        window.setWindowIcon(app.windowIcon())
        window.show()
        return app.exec()
    finally:
        instance_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
