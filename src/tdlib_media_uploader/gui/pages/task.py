# -*- coding: utf-8 -*-
"""Upload queue, current transfer and task details."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.filesystem import stable_path
from ...upload.queue import ACTIVE, RECOVERABLE, AlbumStatus, TaskStatus
from ..theme import status_color
from ..tools import format_eta, format_size, kind_label


class TaskPage(QWidget):
    """Display the live transfer independently of the selected queue task."""

    safe_stop_requested = Signal()
    immediate_stop_requested = Signal()
    disconnect_requested = Signal()
    queue_pause_requested = Signal(bool)
    queue_move_requested = Signal(str, int)
    queue_top_requested = Signal(str)
    queue_remove_requested = Signal(str)
    queue_clear_requested = Signal()
    queue_restore_requested = Signal(str)
    queue_skip_requested = Signal(str)
    queue_recheck_requested = Signal(str)
    queue_inflight_requested = Signal()
    queue_cancel_requested = Signal()
    album_skip_requested = Signal(str, str)
    album_retry_requested = Signal(str, str)

    def __init__(
        self,
        *,
        size_formatter: Callable[[float | int | None], str] | None = None,
        eta_formatter: Callable[[float | int | None], str] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._size_formatter = size_formatter or format_size
        self._eta_formatter = eta_formatter or format_eta
        self._target_label_provider: Callable[[Mapping[str, Any]], Mapping[str, str]] | None = None
        self._live_album_key: str | None = None
        self._live_album_rows: list[str] = []
        self._live_attempt = None
        self._log_entries: list[tuple[str, str]] = []

        self._connection_notice = ""
        self._cooldown_seconds = 0
        self._has_transfer = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 14, 22, 14)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        self.title = QLabel("任务中心")
        self.title.setObjectName("pageTitle")
        title_row.addWidget(self.title)
        title_row.addStretch(1)
        self.stop_button = QPushButton("安全停止")
        self.stop_button.setObjectName("secondaryButton")
        self.stop_button.setToolTip("当前 Album 发送完并保存断点后停止")
        self.stop_button.clicked.connect(self.safe_stop_requested)
        self.immediate_stop_button = QPushButton("立即中断")
        self.immediate_stop_button.setObjectName("dangerButton")
        self.immediate_stop_button.clicked.connect(self.immediate_stop_requested)
        title_row.addWidget(self.stop_button)
        title_row.addWidget(self.immediate_stop_button)
        layout.addLayout(title_row)

        self.task_status = QLabel("无正在运行的任务")
        self.task_status.setObjectName("valueLabel")
        self.active_task_label = QLabel()
        self.active_task_label.setObjectName("valueLabel")
        self.album_label = QLabel()
        self.album_label.setObjectName("mutedLabel")
        self.transfer_notice = QLabel()
        self.transfer_notice.setObjectName("valueLabel")
        self.attempt_label = QLabel()
        self.attempt_label.setObjectName("mutedLabel")
        self.metrics = QLabel()
        self.metrics.setObjectName("mutedLabel")
        self.preliminary_metrics = QLabel()
        self.preliminary_metrics.setObjectName("mutedLabel")
        for label in (
            self.task_status, self.active_task_label, self.album_label, self.transfer_notice, self.attempt_label,
            self.metrics, self.preliminary_metrics,
        ):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        status_row = QHBoxLayout()
        status_row.addWidget(self.active_task_label, 2)
        status_row.addWidget(self.task_status, 1)
        layout.addLayout(status_row)
        group_status = QHBoxLayout()
        group_status.setSpacing(10)
        group_status.addWidget(self.album_label, 1)
        group_status.addWidget(self.transfer_notice, 1)
        layout.addLayout(group_status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        layout.addWidget(self.metrics)
        layout.addWidget(self.attempt_label)
        layout.addWidget(self.preliminary_metrics)
        for widget in (
            self.stop_button, self.immediate_stop_button, self.active_task_label, self.album_label, self.transfer_notice,
            self.metrics, self.attempt_label, self.preliminary_metrics,
        ):
            widget.hide()
        self.stop_button.setEnabled(False)
        self.immediate_stop_button.setEnabled(False)

        body = QHBoxLayout()
        body.setSpacing(18)
        body.addWidget(self._build_queue(), 6)
        self.detail_tabs = QTabWidget()
        self.detail_tabs.setObjectName("taskDetailTabs")
        self.detail_tabs.addTab(self._build_album_details(), "Album 明细")
        self.detail_tabs.addTab(self._build_log(), "实时日志")
        body.addWidget(self.detail_tabs, 5)
        layout.addLayout(body, 1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = event.size().width() < 920
        for column in (0, 4):
            self.queue_list.setColumnHidden(column, compact)
        self.queue_list.setColumnWidth(1, 86 if compact else 110)
        self.queue_list.setColumnWidth(2, 58 if compact else 68)
        self.albums.setColumnHidden(2, compact)
        self.albums.setColumnWidth(1, 86 if compact else 104)
        self.albums.setColumnWidth(3, 58 if compact else 70)

    def _build_album_details(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)

        self.albums = QTreeWidget()
        self.albums.setObjectName("taskAlbumList")
        self.albums.setRootIsDecorated(False)
        self.albums.setHeaderLabels(["Album", "状态", "文件", "进度"])
        self.albums.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.albums.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.albums.setMinimumHeight(70)
        self.albums.setMaximumHeight(145)
        header = self.albums.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for column in (0, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.albums.setColumnWidth(1, 104)
        self.albums.setColumnWidth(3, 70)
        self.albums.itemSelectionChanged.connect(self._on_album_selection_changed)
        self.albums.hide()
        layout.addWidget(self.albums)

        self.album_details_title = QLabel("暂无 Album")
        self.album_details_title.setObjectName("valueLabel")
        self.album_details_title.setTextFormat(Qt.TextFormat.PlainText)
        self.album_details_title.setWordWrap(True)
        self.album_details_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.album_details_title)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.album_skip = QPushButton("跳过 Album")
        self.album_retry = QPushButton("重试该 Album")
        for button in (self.album_skip, self.album_retry):
            button.setObjectName("secondaryButton")
            button.hide()
            actions.addWidget(button)
        self.album_skip.clicked.connect(
            lambda: self.album_skip_requested.emit(self._selected_task(), self._selected_album())
        )
        self.album_retry.clicked.connect(
            lambda: self.album_retry_requested.emit(self._selected_task(), self._selected_album())
        )
        self.album_reason = QPlainTextEdit()
        self.album_reason.setObjectName("taskAlbumReason")
        self.album_reason.setAccessibleName("Album 处理原因")
        self.album_reason.setReadOnly(True)
        self.album_reason.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.album_reason.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.album_reason.setMinimumHeight(44)
        self.album_reason.setMaximumHeight(130)
        self.album_reason.hide()
        layout.addWidget(self.album_reason, 1)

        self.album_files = QListWidget()
        self.album_files.setObjectName("albumFilesList")
        self.album_files.setAccessibleName("所选 Album 的文件")
        self.album_files.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.album_files.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.album_files.setMinimumHeight(34)
        self.album_files.itemActivated.connect(self._show_file_details)
        layout.addWidget(self.album_files, 2)
        layout.addLayout(actions)
        return page

    def _build_log(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.log_filter = QComboBox()
        self.log_filter.addItem("全部日志", "all")
        self.log_filter.addItem("仅警告/错误", "issues")
        self.log_filter.currentIndexChanged.connect(self._rebuild_log_view)
        controls.addWidget(self.log_filter)

        controls.addStretch(1)

        log_options = QPushButton("更多")
        log_options.setObjectName("ghostButton")
        log_options.setAccessibleName("更多日志操作")
        menu = QMenu(log_options)
        self.auto_scroll = menu.addAction("自动滚动")
        self.auto_scroll.setCheckable(True)
        self.auto_scroll.setChecked(True)
        menu.addSeparator()
        menu.addAction("复制日志", self._copy_log)
        menu.addAction("清空日志", self._clear_log)
        log_options.setMenu(menu)
        controls.addWidget(log_options)
        layout.addLayout(controls)

        self.log = QPlainTextEdit()
        self.log.setObjectName("taskLog")
        self.log.setReadOnly(True)
        self.log.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.log.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.log.setMinimumHeight(80)
        self.log.setMaximumBlockCount(3000)
        layout.addWidget(self.log, 1)
        return page

    def set_target_label_provider(
        self,
        provider: Callable[[Mapping[str, Any]], Mapping[str, str]] | None,
    ):
        self._target_label_provider = provider
        self.refresh_target_labels()

    def _format_task_target(self, target: Mapping[str, Any]) -> tuple[str, str]:
        raw = str(target.get("chat_id", ""))
        if target.get("forum_topic_id"):
            raw += f" / {target['forum_topic_id']}"
        if self._target_label_provider is not None:
            cached = dict(self._target_label_provider(target) or {})
            chat_title = str(cached.get("chat_title", "") or "").strip()
            topic_name = str(cached.get("topic_name", "") or "").strip()
            if chat_title or topic_name:
                chat_part = chat_title or str(target.get("chat_id", ""))
                if target.get("forum_topic_id"):
                    topic_part = topic_name or str(target["forum_topic_id"])
                    display = f"{chat_part} / {topic_part}"
                else:
                    display = f"{chat_part}（频道）" if chat_title else raw
                return display, f"{display}\nID: {raw}"
        return raw, raw

    def refresh_target_labels(self):
        for task_id, row in self._queue_rows.items():
            task = self._queue_tasks.get(task_id)
            if task is None:
                continue
            display, tooltip = self._format_task_target(task.target)
            row.setText(4, display)
            row.setToolTip(4, tooltip)

    def _build_queue(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(5)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        title = QLabel("上传队列")
        title.setObjectName("sectionTitle")
        controls.addWidget(title)
        controls.addStretch(1)
        self.queue_toggle = QPushButton("暂停队列")
        self.queue_toggle.setObjectName("secondaryButton")
        self.queue_toggle.clicked.connect(lambda: self.queue_pause_requested.emit(not self._queue_paused))
        controls.addWidget(self.queue_toggle)

        options = QPushButton("更多")
        options.setObjectName("ghostButton")
        options.setAccessibleName("更多队列操作")
        self.queue_menu = QMenu(options)
        options.setMenu(self.queue_menu)
        controls.addWidget(options)
        self.queue_task_details = self.queue_menu.addAction("查看任务详情", self._show_task_details)
        self.queue_up = self.queue_menu.addAction("上移", lambda: self.queue_move_requested.emit(self._selected_task(), -1))
        self.queue_down = self.queue_menu.addAction("下移", lambda: self.queue_move_requested.emit(self._selected_task(), 1))
        self.queue_top = self.queue_menu.addAction("置顶", lambda: self.queue_top_requested.emit(self._selected_task()))
        self.queue_remove = self.queue_menu.addAction("移除", lambda: self.queue_remove_requested.emit(self._selected_task()))
        self.queue_menu.addSeparator()
        self.queue_clear = self.queue_menu.addAction("清理已结束", self.queue_clear_requested.emit)
        self.queue_clear.setEnabled(False)
        self.disconnect_button = self.queue_menu.addAction("断开 Telegram", self.disconnect_requested.emit)
        self.disconnect_button.setEnabled(False)
        layout.addLayout(controls)

        self.queue_list = QTreeWidget()
        self.queue_list.setObjectName("taskQueueList")
        self.queue_list.setRootIsDecorated(False)
        self.queue_list.setAlternatingRowColors(True)
        self.queue_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.queue_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.queue_list.setHeaderLabels(["序", "状态", "进度", "任务", "目标", ""])
        self.queue_list.setMinimumHeight(57)
        header = self.queue_list.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for column in (3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 36), (1, 110), (2, 68), (5, 34)):
            self.queue_list.setColumnWidth(column, width)
        header.moveSection(2, 4)
        self.queue_list.itemSelectionChanged.connect(self._queue_selection_changed)
        self.queue_list.itemClicked.connect(self._open_row_menu)
        layout.addWidget(self.queue_list, 1)

        self.queue_notice = QLabel()
        self.queue_notice.setObjectName("mutedLabel")
        self.queue_notice.setTextFormat(Qt.TextFormat.PlainText)
        self.queue_notice.setWordWrap(True)
        self.queue_notice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.queue_notice)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        actions.addStretch(1)
        self.queue_restore = QPushButton("重新验证并恢复")
        self.queue_skip = QPushButton("跳过此任务")
        self.queue_recheck = QPushButton("重新核对状态")
        self.queue_inflight = QPushButton("前往未确认上传")
        self.queue_cancel = QPushButton("取消检查")
        for button in (self.queue_restore, self.queue_skip, self.queue_recheck,
                       self.queue_inflight, self.queue_cancel):
            button.setObjectName("secondaryButton")
            actions.addWidget(button)
        self.queue_restore.clicked.connect(lambda: self.queue_restore_requested.emit(self._selected_task()))
        self.queue_skip.clicked.connect(lambda: self.queue_skip_requested.emit(self._selected_task()))
        self.queue_recheck.clicked.connect(lambda: self.queue_recheck_requested.emit(self._selected_task()))
        self.queue_inflight.clicked.connect(self.queue_inflight_requested)
        self.queue_cancel.clicked.connect(self.queue_cancel_requested)
        self.queue_cancel.hide()
        layout.addLayout(actions)
        self._queue_paused = False
        self._queue_pending = []
        self._queue_actions_allowed = False
        self._queue_recovering = False
        self._blocked_for_review = False
        self._queue_states = {}
        self._queue_tasks = {}
        self._queue_rows = {}
        self._active_task_id = None
        self._displayed_task = None
        self._album_rows = {}
        self._update_queue_controls()
        return page

    def _open_row_menu(self, item, column):
        if column == 5 and self.queue_list.currentItem() is item:
            rect = self.queue_list.visualItemRect(item)
            self.queue_menu.popup(self.queue_list.viewport().mapToGlobal(rect.bottomRight()))

    def _show_task_details(self):
        task = self._queue_tasks.get(self._selected_task())
        if task is None:
            return
        row = self._queue_rows[task.task_id]
        fields = [
            f"媒体类型：{kind_label(task.kind)}",
            f"状态：{row.text(1)}",
            f"来源目录：{task.source_root}",
            f"目标：{self._format_task_target(task.target)[1]}",
            f"文件：{len(task.items)} · 大小：{self._size_formatter(task.total_bytes)}",
        ]
        if task.error:
            fields.extend(("", "任务处理信息", task.error))
        self._show_text_details("上传任务详情", "\n".join(fields), str(task.source_root))

    def _show_file_details(self, item):
        if item.flags() & Qt.ItemFlag.ItemIsSelectable:
            path = item.data(Qt.ItemDataRole.UserRole + 1) or ""
            text = f"{item.text()}\n\n{path}" if path else item.text()
            self._show_text_details("文件详情", text, path)

    def _show_text_details(self, title, text, path):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(490, 280)
        layout = QVBoxLayout(dialog)
        content = QPlainTextEdit(text)
        content.setReadOnly(True)
        content.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.addWidget(content)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if path:
            copy = buttons.addButton("复制路径", QDialogButtonBox.ButtonRole.ActionRole)
            copy.clicked.connect(lambda: QApplication.clipboard().setText(path))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _selected_task(self):
        row = self.queue_list.currentItem()
        return row.data(0, Qt.ItemDataRole.UserRole) if row is not None else ""

    def select_task(self, task_id):
        row = self._queue_rows.get(task_id)
        if row is not None:
            self.queue_list.setCurrentItem(row)

    def _update_queue_controls(self):
        selected = self._selected_task()
        pending = self._queue_pending
        index = pending.index(selected) if selected in pending else -1
        self.queue_task_details.setEnabled(bool(selected))
        self.queue_up.setEnabled(index > 0)
        self.queue_top.setEnabled(index > 0)
        self.queue_down.setEnabled(0 <= index < len(pending) - 1)
        self.queue_remove.setEnabled(index >= 0)
        status = self._queue_states.get(selected)
        recoverable = status in RECOVERABLE
        review = status is TaskStatus.NEEDS_REVIEW
        self.queue_restore.setVisible(recoverable and not self._queue_recovering)
        self.queue_restore.setEnabled(recoverable and self._queue_actions_allowed)
        self.queue_skip.setVisible(recoverable and not self._queue_recovering)
        self.queue_skip.setEnabled(recoverable and self._queue_actions_allowed)
        self.queue_recheck.setVisible(review and not self._queue_recovering)
        self.queue_recheck.setEnabled(review and self._queue_actions_allowed)
        self.queue_inflight.setVisible((review or self._blocked_for_review) and not self._queue_recovering)

    def _queue_selection_changed(self):
        self._update_queue_controls()
        self._render_albums(self._queue_tasks.get(self._selected_task()))

    def _selected_album(self):
        row = self.albums.currentItem()
        return row.data(0, Qt.ItemDataRole.UserRole) if row is not None else ""

    def _on_album_selection_changed(self):
        self._update_album_controls()
        self._render_album_files()

    def _update_album_controls(self):
        task = self._displayed_task
        row = self.albums.currentItem()
        if task is None or row is None:
            self.album_skip.hide()
            self.album_retry.setText("重试该 Album")
            self.album_retry.setToolTip("")
            self.album_retry.setEnabled(False)
            self.album_retry.hide()
            return
        outcome = row.data(1, Qt.ItemDataRole.UserRole)
        scope, _ = task.album_retry_policy(outcome.album_key)
        if scope == "review_required":
            self.album_retry.setText("需要人工核对")
            self.album_retry.setToolTip("存在未确认、持久化异常或缺少阶段信息的旧记录，请先核对或使用整任务“重新验证并恢复”")
        elif scope == "failed_files":
            self.album_retry.setText("仅重试失败文件")
            self.album_retry.setToolTip("仅重试部分投递中明确失败且已解除保护的未确认成员")
        else:
            self.album_retry.setText("重试该 Album")
            self.album_retry.setToolTip("按原 Album 顺序、目标与标题重试全部未确认成员")
        can_skip = task.status in RECOVERABLE | {TaskStatus.PENDING} and outcome.status not in {
            AlbumStatus.COMPLETED, AlbumStatus.UNKNOWN, AlbumStatus.SKIPPED,
        }
        can_retry = task.status in RECOVERABLE and scope in {"album", "failed_files"}
        self.album_skip.setVisible(can_skip and self._queue_actions_allowed)
        self.album_retry.setVisible(can_retry and self._queue_actions_allowed)
        self.album_skip.setEnabled(can_skip and self._queue_actions_allowed)
        self.album_retry.setEnabled(can_retry and self._queue_actions_allowed)

    def _update_album_details(self, live):
        row = self.albums.currentItem()
        if live:
            name = self.album_label.text()
            title = f"当前 Album · {name}" if name else "当前 Album"
        elif row is not None:
            title = f"{row.text(0)} · {row.text(1)}"
        elif self._has_transfer and self._selected_task() == self._active_task_id:
            title = "等待当前 Album"
        else:
            title = "暂无 Album"
        self.album_details_title.setText(title)
        self.album_details_title.setToolTip(self.album_label.toolTip() if live else title)
        reason = row.toolTip(3) if row is not None else ""
        if live and self._live_attempt is not None and self._live_attempt.album_key == self._live_album_key:
            reason = self._live_attempt.last_error or ""
        if reason != self.album_reason.toPlainText():
            self.album_reason.setPlainText(reason)
        self.album_reason.setVisible(bool(reason))

    def _is_live_album(self):
        if not self._live_album_rows:
            return False
        task = self._displayed_task
        if task is None:
            return self._has_transfer
        album_key = self._selected_album()
        return task.task_id == self._active_task_id and (
            not album_key or not self._live_album_key or album_key == self._live_album_key
        )

    def _render_album_files(self):
        task = self._displayed_task
        album_key = self._selected_album()
        live = self._is_live_album()
        self._update_album_details(live)
        self.album_files.clear()
        if live:
            for text in self._live_album_rows:
                entry = QListWidgetItem(text)
                entry.setToolTip(text)
                self.album_files.addItem(entry)
            return
        if task is None:
            return
        plan = next((entry for entry in task.plans if entry.key == album_key), None)
        if plan is None:
            return
        outcomes = task.album_statuses()
        outcome = outcomes.get(plan.key)
        confirmed = set(task.confirmed_paths) | task.known_confirmed
        failed = set(outcome.failed_paths) if outcome is not None else set()
        deferred = set(outcome.deferred_paths) if outcome is not None else set()
        for item in plan.items:
            key = stable_path(item.path)
            if outcome is not None and outcome.status is AlbumStatus.UNKNOWN:
                state_text = "待核对"
            elif key in confirmed:
                state_text = "已完成"
            elif (outcome is not None and outcome.status is AlbumStatus.SKIPPED) or task.status is TaskStatus.SKIPPED:
                state_text = "已跳过"
            elif key in failed:
                state_text = "失败"
            elif key in deferred:
                state_text = "暂缓"
            else:
                state_text = "待上传"
            entry = QListWidgetItem(
                f"{kind_label(item.media_kind)} · {state_text} · {self._size_formatter(item.snapshot.size)} · {item.path.name}"
            )
            entry.setData(Qt.ItemDataRole.UserRole, state_text)
            entry.setData(Qt.ItemDataRole.UserRole + 1, str(item.path))
            entry.setForeground(QColor(status_color(state_text)))
            entry.setToolTip(str(item.path))
            self.album_files.addItem(entry)

    def _render_albums(self, task):
        active = task is not None and task.task_id == self._active_task_id
        self.albums.setVisible(task is not None and not active and len(task.plans) > 1)
        if task == self._displayed_task:
            self._update_album_controls()
            self._render_album_files()
            return
        selection = self._selected_album() if (
            task is not None and self._displayed_task is not None
            and task.task_id == self._displayed_task.task_id
        ) else None
        if active:
            selection = self._live_album_key or selection
        self._displayed_task = task
        self.albums.blockSignals(True)
        self.albums.clear()
        self._album_rows.clear()
        if task is not None:
            states = task.album_statuses()
            labels = {
                AlbumStatus.PENDING: "待上传", AlbumStatus.COMPLETED: "已完成",
                AlbumStatus.PARTIAL: "部分完成", AlbumStatus.DEFERRED: "暂缓",
                AlbumStatus.FAILED: "失败", AlbumStatus.UNKNOWN: "待核对", AlbumStatus.SKIPPED: "已跳过",
            }
            confirmed = set(task.confirmed_paths) | task.known_confirmed
            for plan in task.plans:
                outcome = states[plan.key]
                names = [item.path.name for item in plan.items]
                summary = "、".join(names[:3])
                if len(names) > 3:
                    summary += f" 等 {len(names)} 个"
                done = sum(stable_path(item.path) in confirmed for item in plan.items)
                status_text = labels[outcome.status]
                row = QTreeWidgetItem([
                    f"{plan.group_label} · {plan.number}", status_text,
                    summary, f"{done}/{len(names)}",
                ])
                row.setData(0, Qt.ItemDataRole.UserRole, plan.key)
                row.setData(1, Qt.ItemDataRole.UserRole, outcome)
                row.setForeground(1, QColor(status_color(status_text)))
                row.setToolTip(2, "\n".join(names))
                row.setToolTip(3, outcome.error or "")
                self.albums.addTopLevelItem(row)
                self._album_rows[plan.key] = row
                if plan.key == selection:
                    self.albums.setCurrentItem(row)
            if not active and self.albums.currentItem() is None:
                self.albums.setCurrentItem(self.albums.topLevelItem(0))
        self.albums.blockSignals(False)
        self._update_album_controls()
        self._render_album_files()

    @Slot(str, bool)
    def show_recovery_progress(self, message, cancelable):
        self.queue_notice.setText(message)
        self.queue_cancel.setEnabled(cancelable)

    def show_queue(self, store, blocked=False, *, recovering=False):
        selected = self._selected_task()
        self._queue_tasks = {task.task_id: task for task in store.tasks}
        self._active_task_id = next((task.task_id for task in store.tasks if task.status in ACTIVE), None)
        if selected not in self._queue_tasks and store.tasks:
            selected = self._active_task_id or store.tasks[0].task_id
        self._queue_paused = store.paused
        self._queue_recovering = recovering
        self._queue_pending = []
        self._queue_states = {task.task_id: task.status for task in store.tasks}
        self._blocked_for_review = TaskStatus.NEEDS_REVIEW in self._queue_states.values()
        self._queue_actions_allowed = not (
            recovering or blocked or store.error or any(status in ACTIVE for status in self._queue_states.values())
        )
        self.queue_list.blockSignals(True)
        scroll = self.queue_list.verticalScrollBar().value()
        self.queue_list.clear()
        self._queue_rows.clear()
        labels = {
            TaskStatus.PENDING: "等待中", TaskStatus.PREPARING: "准备中", TaskStatus.RUNNING: "上传中",
            TaskStatus.COMPLETED: "已完成", TaskStatus.FAILED: "失败", TaskStatus.INTERRUPTED: "已中断",
            TaskStatus.NEEDS_REVIEW: "待核对", TaskStatus.SKIPPED: "已跳过",
        }
        for index, task in enumerate(store.tasks, 1):
            state = "部分完成" if task.status is TaskStatus.FAILED and task.outcome == "PARTIAL" else labels[task.status]
            status_tooltip = task.error or state
            if task.task_id == self._active_task_id and self._cooldown_seconds:
                state = "冷却中"
                status_tooltip = f"{self._cooldown_seconds} 秒后重试"
            display_target, target_tooltip = self._format_task_target(task.target)
            name = task.source_root.name or str(task.source_root)
            progress = (
                f"{self.progress.value() / 10:.0f}%"
                if task.task_id == self._active_task_id and self._has_transfer
                else "100%" if task.status is TaskStatus.COMPLETED else "—"
            )
            row = QTreeWidgetItem([
                str(index), state, progress, f"{name} · {kind_label(task.kind)}", display_target, "⋯",
            ])
            row.setData(0, Qt.ItemDataRole.UserRole, task.task_id)
            row.setData(1, Qt.ItemDataRole.UserRole, state)
            row.setForeground(1, QColor(status_color(state)))
            row.setToolTip(1, status_tooltip)
            row.setToolTip(3, str(task.source_root))
            row.setToolTip(4, target_tooltip)
            row.setToolTip(5, "任务操作")
            row.setTextAlignment(5, Qt.AlignmentFlag.AlignCenter)
            self.queue_list.addTopLevelItem(row)
            self._queue_rows[task.task_id] = row
            if task.status is TaskStatus.PENDING and not (recovering or blocked or store.error):
                self._queue_pending.append(task.task_id)
            if task.task_id == selected:
                self.queue_list.setCurrentItem(row)
        self.queue_list.blockSignals(False)
        if scroll:
            self.queue_list.doItemsLayout()
            self.queue_list.verticalScrollBar().setValue(scroll)
        has_finished = any(
            status in {TaskStatus.COMPLETED, TaskStatus.SKIPPED}
            for status in self._queue_states.values()
        )
        self.queue_clear.setEnabled(has_finished and not recovering and not blocked and not store.error)
        self.queue_toggle.setText("继续队列" if store.paused else "暂停队列")
        self.queue_toggle.setEnabled(not (
            recovering or blocked or store.error
            or store.paused and any(status in ACTIVE for status in self._queue_states.values())
        ))
        self.queue_cancel.setVisible(recovering)
        if not recovering and not blocked:
            self.queue_notice.setText(store.error or store.blocked_reason or (
                "队列已暂停。继续后按顺序执行等待任务。" if store.paused else
                f"{len(self._queue_pending)} 个任务等待上传" if self._queue_pending else
                "没有等待上传的任务" if store.tasks else "暂无上传任务"
            ))
        self._queue_selection_changed()

    def show_album_attempt(self, event):
        self._live_attempt = event
        text = f"Album {event.album_number} · 第 {event.attempt}/{event.max_attempts} 次尝试"
        if event.last_error:
            text += f" · 上次失败 {event.last_error.splitlines()[0][:160]}"
        if event.retry_in_seconds is not None:
            text += f" · {event.retry_in_seconds} 秒后重试"
        self.attempt_label.setText(text)
        self.attempt_label.show()
        self._show_queue_cooldown(self._cooldown_seconds or event.retry_in_seconds)
        if self._selected_task() == self._active_task_id:
            row = self._album_rows.get(event.album_key)
            if row is not None:
                status = "冷却中" if event.retry_in_seconds is not None else "上传中"
                row.setText(1, status)
                row.setForeground(1, QColor(status_color(status)))
                row.setToolTip(3, event.last_error or "")
                if row is self.albums.currentItem():
                    self._update_album_details(self._is_live_album())

    def start_session(self, kind: str, result: dict):
        self._has_transfer = True
        self._cooldown_seconds = 0
        self.preliminary_metrics.hide()
        self._live_album_key = None
        self._live_album_rows = []
        self._live_attempt = None
        name = result.get("task_name") or kind_label(kind)
        self.active_task_label.setText(f"当前任务：{name}" if len(name) <= 72 else f"当前任务：{name[:71]}…")
        self.active_task_label.setToolTip(name)
        self.task_status.setText("正在启动…")
        self.stop_button.setEnabled(True)
        self.immediate_stop_button.setEnabled(True)
        for widget in (self.active_task_label, self.stop_button, self.immediate_stop_button, self.metrics):
            widget.show()
        self.progress.setValue(0)
        self.album_label.hide()
        self.attempt_label.hide()
        self.metrics.setText(
            f"待上传 {result['pending_files']} 个 · "
            f"{self._size_formatter(result['pending_bytes'])} · "
            f"{result['album_count']} 个 Album"
        )
        self._update_transfer_notice()
        self._render_album_files()
        self._clear_log()

    def _clear_log(self):
        self._log_entries.clear()
        self.log.clear()

    def _copy_log(self):
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.log.toPlainText())

    def _log_matches_filter(self, level: str) -> bool:
        mode = self.log_filter.currentData() if hasattr(self, "log_filter") else "all"
        if mode == "issues":
            return level in {"warning", "error"}
        return True

    def _append_log_line(self, level: str, line: str):
        self._log_entries.append((level, line))
        if len(self._log_entries) > 3000:
            self._log_entries = self._log_entries[-3000:]
        if self._log_matches_filter(level):
            self.log.appendPlainText(line)
            if self.auto_scroll.isChecked():
                self.log.moveCursor(QTextCursor.MoveOperation.End)

    def _rebuild_log_view(self):
        self.log.setUpdatesEnabled(False)
        try:
            self.log.clear()
            lines = [line for level, line in self._log_entries if self._log_matches_filter(level)]
            if lines:
                self.log.setPlainText("\n".join(lines))
                if self.auto_scroll.isChecked():
                    self.log.moveCursor(QTextCursor.MoveOperation.End)
        finally:
            self.log.setUpdatesEnabled(True)

    @Slot(str, str)
    def add_message(self, level: str, text: str):
        prefix = {"success": "✓", "warning": "!", "error": "✗", "info": "ℹ"}.get(level, "·")
        self._append_log_line(level, f"{prefix} {text}")

    @Slot(object)
    def show_album(self, payload: dict):
        title = str(payload.get("title") or "")
        self.album_label.setText(title if len(title) <= 105 else f"{title[:104]}…")
        self.album_label.setToolTip(" · ".join(part for part in (title, payload.get("subtitle")) if part))
        self.album_label.setVisible(bool(title))
        if self.task_status.text() == "正在启动…":
            self.task_status.setText("上传中")
        self._live_album_key = str(payload.get("album_key") or "") or None
        self._live_album_rows = [str(row) for row in payload.get("rows", [])]
        if self._displayed_task is None or self._displayed_task.task_id == self._active_task_id:
            self.albums.blockSignals(True)
            self.albums.setCurrentItem(self._album_rows.get(self._live_album_key))
            self.albums.blockSignals(False)
            self._update_album_controls()
            self._render_album_files()

    @Slot(object)
    def show_progress(self, payload: dict):
        ratio = max(0.0, min(float(payload.get("ratio", 0)), 1.0))
        self.progress.setValue(round(ratio * 1000))
        speed = float(payload.get("speed", 0) or 0)
        self.metrics.setText(
            f"速度 {self._size_formatter(speed)}/s · "
            f"剩余 {self._eta_formatter(payload.get('eta'))} · "
            f"文件 {payload.get('done_files', 0)}/{payload.get('total_files', 0)} · "
            f"已传 {self._size_formatter(payload.get('done_bytes', 0))} / "
            f"{self._size_formatter(payload.get('total_bytes', 0))}"
        )
        active_row = self._queue_rows.get(self._active_task_id)
        if active_row is not None:
            active_row.setText(2, f"{ratio:.0%}")
        if self._selected_task() == self._active_task_id:
            row = self._album_rows.get(payload.get("album_key"))
            if row is not None and "album_ratio" in payload:
                row.setText(3, f"{float(payload['album_ratio']):.0%}")

    @Slot(int)
    def show_rate_limit(self, seconds: int):
        self._cooldown_seconds = seconds
        self._update_transfer_notice()
        self._show_queue_cooldown(seconds or None)

    def set_connection_status(self, label: str, connected: bool):
        self._connection_notice = "" if connected else label
        self._update_transfer_notice()

    def _update_transfer_notice(self):
        cooldown = f"Telegram 限流，{self._cooldown_seconds} 秒后重试" if self._cooldown_seconds else ""
        text = " · ".join(part for part in (cooldown, self._connection_notice) if part)
        self.transfer_notice.setText(text)
        self.transfer_notice.setVisible(self._has_transfer and bool(text))

    def _show_queue_cooldown(self, seconds):
        row = self._queue_rows.get(self._active_task_id)
        if row is not None:
            status = "上传中" if seconds is None else "冷却中"
            row.setText(1, status)
            row.setData(1, Qt.ItemDataRole.UserRole, status)
            row.setForeground(1, QColor(status_color(status)))
            row.setToolTip(1, status if seconds is None else f"{seconds} 秒后重试")

    @Slot(object)
    def show_preliminary_progress(self, event):
        size = self._size_formatter
        self.preliminary_metrics.setText(
            f"预上传 {size(event.uploaded_bytes)} / {size(event.total_bytes)}"
            f" · {size(event.bytes_per_second)}/s"
        )
        self.preliminary_metrics.show()

    def finish_session(self, success: bool, message: str):
        self._has_transfer = False
        self._cooldown_seconds = 0
        self._live_album_key = None
        self._live_album_rows = []
        self._live_attempt = None
        if success:
            self.progress.setValue(self.progress.maximum())
        self._update_transfer_notice()
        self.stop_button.setEnabled(False)
        self.immediate_stop_button.setEnabled(False)
        for widget in (
            self.stop_button, self.immediate_stop_button, self.active_task_label, self.album_label,
            self.metrics, self.attempt_label, self.preliminary_metrics,
        ):
            widget.hide()
        self.task_status.setText("已完成" if success else message)
        self._render_album_files()
        self._append_log_line("success" if success else "warning", ("✓ " if success else "! ") + message)

    def refresh_theme(self):
        """Re-apply dynamic theme styling."""
        for row in self._queue_rows.values():
            state = row.data(1, Qt.ItemDataRole.UserRole) or row.text(1)
            row.setForeground(1, QColor(status_color(str(state))))
        for row in self._album_rows.values():
            row.setForeground(1, QColor(status_color(row.text(1))))
        for i in range(self.album_files.count()):
            item = self.album_files.item(i)
            if item is not None:
                state = item.data(Qt.ItemDataRole.UserRole)
                if state:
                    item.setForeground(QColor(status_color(str(state))))
__all__ = ["TaskPage", "format_eta", "format_size", "kind_label"]
