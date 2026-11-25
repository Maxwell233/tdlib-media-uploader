# -*- coding: utf-8 -*-
"""Modern task center page for TDLib Media Uploader Beta 4."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
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
    """Mission Control center displaying real-time upload progress, speed and diagnostics."""

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
        self._log_entries: list[tuple[str, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)

        # Header Row
        title_row = QHBoxLayout()
        self.title = QLabel("任务中心")
        self.title.setObjectName("pageTitle")
        self.task_status = QLabel("无正在运行的任务")
        self.task_status.setObjectName("valueLabel")

        title_row.addWidget(self.title)
        title_row.addStretch(1)
        title_row.addWidget(self.task_status)
        layout.addLayout(title_row)

        self.main_splitter = QSplitter(Qt.Orientation.Vertical)
        self.main_splitter.setChildrenCollapsible(False)
        queue_card = self._build_queue()
        self.main_splitter.addWidget(queue_card)

        lower_container = QWidget()
        lower_layout = QVBoxLayout(lower_container)
        lower_layout.setContentsMargins(0, 4, 0, 0)
        lower_layout.setSpacing(12)

        # 1. Dual Progress & Real-time Metrics Card (No QGroupBox)
        progress_card = QFrame()
        progress_card.setObjectName("surfaceCard")
        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(18, 14, 18, 14)
        progress_layout.setSpacing(8)

        # Album title and progress bar
        self.album_label = QLabel("当前 Album：尚未开始")
        self.album_label.setObjectName("sectionTitle")
        progress_layout.addWidget(self.album_label)
        self.attempt_label = QLabel()
        self.attempt_label.setObjectName("mutedLabel")
        self.attempt_label.setWordWrap(True)
        self.attempt_label.hide()
        progress_layout.addWidget(self.attempt_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        progress_layout.addWidget(self.progress)

        # Metrics bar
        self.metrics = QLabel("速度 — · Mbps — · ETA --:-- · 文件 0/0 · 已传 0 B")
        self.metrics.setObjectName("mutedLabel")
        progress_layout.addWidget(self.metrics)
        self.preliminary_metrics = QLabel()
        self.preliminary_metrics.setObjectName("mutedLabel")
        self.preliminary_metrics.hide()
        progress_layout.addWidget(self.preliminary_metrics)

        lower_layout.addWidget(progress_card)

        # 2. Split Area: Current Album Files + Live Upload Log Terminal
        self.detail_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.detail_splitter.setChildrenCollapsible(False)

        # Current Album files panel
        album_card = QFrame()
        album_card.setObjectName("surfaceCard")
        album_layout = QVBoxLayout(album_card)
        album_layout.setContentsMargins(14, 12, 14, 12)
        album_layout.setSpacing(8)

        album_title = QLabel("任务的 Album")
        album_title.setObjectName("sectionTitle")
        album_layout.addWidget(album_title)
        self.albums = QTreeWidget()
        self.albums.setRootIsDecorated(False)
        self.albums.setHeaderLabels(["Album", "状态", "文件", "进度", "原因"])
        for column in (2, 4):
            self.albums.header().setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self.albums.itemSelectionChanged.connect(self._on_album_selection_changed)
        album_layout.addWidget(self.albums, 2)

        album_actions = QHBoxLayout()
        self.album_skip = QPushButton("跳过 Album")
        self.album_retry = QPushButton("仅重试失败文件")
        for button in (self.album_skip, self.album_retry):
            button.setObjectName("secondaryButton")
            button.setEnabled(False)
            album_actions.addWidget(button)
        self.album_skip.clicked.connect(
            lambda: self.album_skip_requested.emit(self._selected_task(), self._selected_album())
        )
        self.album_retry.clicked.connect(
            lambda: self.album_retry_requested.emit(self._selected_task(), self._selected_album())
        )
        album_layout.addLayout(album_actions)

        self.album_files = QListWidget()
        self.album_files.setObjectName("albumFilesList")
        self.album_files.setAlternatingRowColors(True)
        album_layout.addWidget(self.album_files, 1)
        self.detail_splitter.addWidget(album_card)

        # Live terminal log panel
        log_card = QFrame()
        log_card.setObjectName("surfaceCard")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(14, 12, 14, 12)
        log_layout.setSpacing(8)

        log_header = QHBoxLayout()
        log_header.setSpacing(8)
        log_title = QLabel("实时上传日志")
        log_title.setObjectName("sectionTitle")
        log_header.addWidget(log_title)
        log_header.addStretch(1)

        self.log_filter = QComboBox()
        self.log_filter.addItem("全部日志", "all")
        self.log_filter.addItem("仅警告/错误", "issues")
        self.log_filter.currentIndexChanged.connect(self._rebuild_log_view)
        log_header.addWidget(self.log_filter)

        self.auto_scroll_check = QCheckBox("自动滚动")
        self.auto_scroll_check.setChecked(True)
        log_header.addWidget(self.auto_scroll_check)

        self.copy_log_btn = QPushButton("复制日志")
        self.copy_log_btn.setObjectName("ghostButton")
        self.copy_log_btn.clicked.connect(self._copy_log)
        log_header.addWidget(self.copy_log_btn)

        self.clear_log_btn = QPushButton("清空日志")
        self.clear_log_btn.setObjectName("ghostButton")
        self.clear_log_btn.clicked.connect(self._clear_log)
        log_header.addWidget(self.clear_log_btn)
        log_layout.addLayout(log_header)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        log_layout.addWidget(self.log, 1)
        self.detail_splitter.addWidget(log_card)
        self.detail_splitter.setStretchFactor(0, 2)
        self.detail_splitter.setStretchFactor(1, 3)
        self.detail_splitter.setSizes([380, 580])

        lower_layout.addWidget(self.detail_splitter, 1)
        self.main_splitter.addWidget(lower_container)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 3)
        self.main_splitter.setSizes([210, 470])
        layout.addWidget(self.main_splitter, 1)

        # 3. Bottom Action Bar (Semantic secondary vs danger button)
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        bottom.addStretch(1)

        self.stop_button = QPushButton("安全停止（当前 Album 发送完毕后退出）")
        self.stop_button.setObjectName("secondaryButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.safe_stop_requested)

        self.immediate_stop_button = QPushButton("立即中断")
        self.immediate_stop_button.setObjectName("dangerButton")
        self.immediate_stop_button.setEnabled(False)
        self.immediate_stop_button.clicked.connect(self.immediate_stop_requested)

        bottom.addWidget(self.stop_button)
        bottom.addWidget(self.immediate_stop_button)
        layout.addLayout(bottom)

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

    def _build_queue(self) -> QFrame:
        card = QFrame()
        card.setObjectName("surfaceCard")
        queue_layout = QVBoxLayout(card)
        queue_layout.setContentsMargins(14, 12, 14, 12)
        queue_layout.setSpacing(8)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        title = QLabel("上传队列")
        title.setObjectName("sectionTitle")
        controls.addWidget(title)
        controls.addStretch(1)
        self.queue_toggle = QPushButton("暂停队列")
        self.queue_up = QPushButton("上移")
        self.queue_down = QPushButton("下移")
        self.queue_top = QPushButton("置顶")
        self.queue_remove = QPushButton("移除")
        self.disconnect_button = QPushButton("断开 Telegram")
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.clicked.connect(self.disconnect_requested)
        for button in (
            self.queue_toggle, self.queue_up, self.queue_down, self.queue_top, self.queue_remove, self.disconnect_button,
        ):
            button.setObjectName("secondaryButton")
            controls.addWidget(button)
        self.queue_toggle.clicked.connect(lambda: self.queue_pause_requested.emit(not self._queue_paused))
        self.queue_up.clicked.connect(lambda: self.queue_move_requested.emit(self._selected_task(), -1))
        self.queue_down.clicked.connect(lambda: self.queue_move_requested.emit(self._selected_task(), 1))
        self.queue_top.clicked.connect(lambda: self.queue_top_requested.emit(self._selected_task()))
        self.queue_remove.clicked.connect(lambda: self.queue_remove_requested.emit(self._selected_task()))
        queue_layout.addLayout(controls)

        self.queue_notice = QLabel()
        self.queue_notice.setObjectName("mutedLabel")
        self.queue_notice.setWordWrap(True)
        queue_layout.addWidget(self.queue_notice)

        self.queue_list = QTreeWidget()
        self.queue_list.setRootIsDecorated(False)
        self.queue_list.setAlternatingRowColors(True)
        self.queue_list.setHeaderLabels(["顺序", "状态", "类型", "来源目录", "上传目标", "文件", "大小"])
        self.queue_list.setMinimumHeight(90)
        header = self.queue_list.header()
        for index in (0, 2, 5, 6):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setStretchLastSection(False)
        self.queue_list.setColumnWidth(1, 96)
        self.queue_list.setColumnWidth(4, 168)
        self.queue_list.itemSelectionChanged.connect(self._queue_selection_changed)
        queue_layout.addWidget(self.queue_list, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.queue_clear = QPushButton("清理已结束")
        self.queue_clear.setObjectName("ghostButton")
        self.queue_clear.setToolTip("从列表中移除已完成和已跳过的任务")
        self.queue_clear.setEnabled(False)
        self.queue_clear.clicked.connect(self.queue_clear_requested)
        actions.addWidget(self.queue_clear)
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
        queue_layout.addLayout(actions)
        self._queue_paused = False
        self._queue_pending = []
        self._queue_actions_allowed = False
        self._queue_states = {}
        self._queue_tasks = {}
        self._queue_rows = {}
        self._active_task_id = None
        self._displayed_task = None
        self._album_rows = {}
        self._update_queue_controls()
        return card

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
        self.queue_up.setEnabled(index > 0)
        self.queue_top.setEnabled(index > 0)
        self.queue_down.setEnabled(0 <= index < len(pending) - 1)
        self.queue_remove.setEnabled(index >= 0)
        status = self._queue_states.get(selected)
        recoverable = status in RECOVERABLE
        review = status is TaskStatus.NEEDS_REVIEW
        self.queue_restore.setVisible(recoverable)
        self.queue_restore.setEnabled(recoverable and self._queue_actions_allowed)
        self.queue_skip.setVisible(recoverable)
        self.queue_skip.setEnabled(recoverable and self._queue_actions_allowed)
        self.queue_recheck.setVisible(review)
        self.queue_recheck.setEnabled(review and self._queue_actions_allowed)
        self.queue_inflight.setVisible(review)

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
        if not self._queue_actions_allowed or task is None or row is None:
            self.album_skip.setEnabled(False)
            self.album_retry.setEnabled(False)
            return
        outcome = row.data(1, Qt.ItemDataRole.UserRole)
        actionable = task.status in RECOVERABLE | {TaskStatus.PENDING}
        self.album_skip.setEnabled(actionable and outcome.status not in {
            AlbumStatus.COMPLETED, AlbumStatus.UNKNOWN, AlbumStatus.SKIPPED,
        })
        self.album_retry.setEnabled(
            task.status in RECOVERABLE and bool(outcome.failed_paths)
            and outcome.status in {AlbumStatus.FAILED, AlbumStatus.PARTIAL}
        )

    def _render_album_files(self):
        self.album_files.clear()
        task = self._displayed_task
        album_key = self._selected_album()
        is_live_view = (
            bool(self._live_album_rows)
            and (
                task is None
                or (
                    task.task_id == self._active_task_id
                    and (not album_key or not self._live_album_key or album_key == self._live_album_key)
                )
            )
        )
        if is_live_view:
            for row in self._live_album_rows:
                self.album_files.addItem(str(row))
            return
        if task is None:
            return
        plan = next((entry for entry in task.plans if entry.key == album_key), None)
        if plan is None:
            hint = QListWidgetItem("点击上方任意 Album 查看其中的文件明细")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            hint.setForeground(QColor(status_color("MUTED")))
            self.album_files.addItem(hint)
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
            entry.setForeground(QColor(status_color(state_text)))
            entry.setToolTip(str(item.path))
            self.album_files.addItem(entry)

    def _render_albums(self, task):
        if task == self._displayed_task:
            self._update_album_controls()
            self._render_album_files()
            return
        selection = self._selected_album()
        self._displayed_task = task
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
                    summary, f"{done}/{len(names)}", outcome.error or "",
                ])
                row.setData(0, Qt.ItemDataRole.UserRole, plan.key)
                row.setData(1, Qt.ItemDataRole.UserRole, outcome)
                row.setForeground(1, QColor(status_color(status_text)))
                row.setToolTip(2, "\n".join(names))
                row.setToolTip(4, outcome.error or "")
                self.albums.addTopLevelItem(row)
                self._album_rows[plan.key] = row
                if plan.key == selection:
                    self.albums.setCurrentItem(row)
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
        if not selected and store.tasks:
            selected = self._active_task_id or store.tasks[0].task_id
        self._queue_paused = store.paused
        self._queue_pending = []
        self._queue_states = {task.task_id: task.status for task in store.tasks}
        self._queue_actions_allowed = not (
            recovering or blocked or store.error or any(status in ACTIVE for status in self._queue_states.values())
        )
        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        self._queue_rows.clear()
        labels = {
            TaskStatus.PENDING: "等待中", TaskStatus.PREPARING: "准备中", TaskStatus.RUNNING: "上传中",
            TaskStatus.COMPLETED: "已完成", TaskStatus.FAILED: "失败", TaskStatus.INTERRUPTED: "已中断",
            TaskStatus.NEEDS_REVIEW: "待核对", TaskStatus.SKIPPED: "已跳过",
        }
        for index, task in enumerate(store.tasks, 1):
            state = "部分完成" if task.status is TaskStatus.FAILED and task.outcome == "PARTIAL" else labels[task.status]
            display_target, target_tooltip = self._format_task_target(task.target)
            row = QTreeWidgetItem([
                str(index), state, kind_label(task.kind), str(task.source_root),
                display_target, str(len(task.items)), self._size_formatter(task.total_bytes),
            ])
            row.setData(0, Qt.ItemDataRole.UserRole, task.task_id)
            row.setData(1, Qt.ItemDataRole.UserRole, state)
            row.setForeground(1, QColor(status_color(state)))
            row.setToolTip(1, task.error or state)
            row.setToolTip(3, str(task.source_root))
            row.setToolTip(4, target_tooltip)
            self.queue_list.addTopLevelItem(row)
            self._queue_rows[task.task_id] = row
            if task.status is TaskStatus.PENDING and not (recovering or blocked or store.error):
                self._queue_pending.append(task.task_id)
            if task.task_id == selected:
                self.queue_list.setCurrentItem(row)
        self.queue_list.blockSignals(False)
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
                f"{len(self._queue_pending)} 个任务等待上传"
            ))
        self._queue_selection_changed()

    def show_album_attempt(self, event):
        text = f"Album {event.album_number} · 第 {event.attempt}/{event.max_attempts} 次尝试"
        if event.last_error:
            text += f" · 上次失败 {event.last_error.splitlines()[0][:160]}"
        if event.retry_in_seconds is not None:
            text += f" · {event.retry_in_seconds} 秒后重试"
        self.attempt_label.setText(text)
        self.attempt_label.show()
        self._show_queue_cooldown(event.retry_in_seconds)
        if self._selected_task() == self._active_task_id:
            row = self._album_rows.get(event.album_key)
            if row is not None:
                status = "冷却中" if event.retry_in_seconds is not None else "上传中"
                row.setText(1, status)
                row.setForeground(1, QColor(status_color(status)))
                row.setText(4, event.last_error or "")

    def start_session(self, kind: str, result: dict):
        self.preliminary_metrics.hide()
        self._live_album_key = None
        self._live_album_rows = []
        self.title.setText(f"任务中心 · {kind_label(kind)}")
        self.task_status.setText("正在启动…")
        self.stop_button.setEnabled(True)
        self.immediate_stop_button.setEnabled(True)
        self.progress.setValue(0)
        self.album_label.setText("当前 Album：尚未开始")
        self.attempt_label.hide()
        self.metrics.setText(
            f"待上传 {result['pending_files']} 个 · "
            f"{self._size_formatter(result['pending_bytes'])} · "
            f"{result['album_count']} 个 Album"
        )
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
            if self.auto_scroll_check.isChecked():
                self.log.moveCursor(QTextCursor.MoveOperation.End)

    def _rebuild_log_view(self):
        self.log.setUpdatesEnabled(False)
        try:
            self.log.clear()
            lines = [line for level, line in self._log_entries if self._log_matches_filter(level)]
            if lines:
                self.log.setPlainText("\n".join(lines))
                if self.auto_scroll_check.isChecked():
                    self.log.moveCursor(QTextCursor.MoveOperation.End)
        finally:
            self.log.setUpdatesEnabled(True)

    @Slot(str, str)
    def add_message(self, level: str, text: str):
        prefix = {"success": "✓", "warning": "!", "error": "✗", "info": "ℹ"}.get(level, "·")
        self._append_log_line(level, f"{prefix} {text}")
        self.task_status.setText(text.splitlines()[0][:100] if text else "运行中")

    @Slot(object)
    def show_album(self, payload: dict):
        self.album_label.setText(f"{payload.get('title', '')} · {payload.get('subtitle', '')}")
        self._live_album_key = str(payload.get("album_key") or "") or None
        self._live_album_rows = [str(row) for row in payload.get("rows", [])]
        self._render_album_files()

    @Slot(object)
    def show_progress(self, payload: dict):
        ratio = max(0.0, min(float(payload.get("ratio", 0)), 1.0))
        self.progress.setValue(round(ratio * 1000))
        speed = float(payload.get("speed", 0) or 0)
        mbps = speed * 8 / 1_000_000
        self.metrics.setText(
            f"速度 {self._size_formatter(speed)}/s · {mbps:,.1f} Mbps · "
            f"ETA {self._eta_formatter(payload.get('eta'))} · "
            f"Album {payload.get('album_number', 0)}/{payload.get('album_total', 0)} · "
            f"文件 {payload.get('done_files', 0)}/{payload.get('total_files', 0)} · "
            f"已传 {self._size_formatter(payload.get('done_bytes', 0))} / "
            f"{self._size_formatter(payload.get('total_bytes', 0))}"
        )
        if self._selected_task() == self._active_task_id:
            row = self._album_rows.get(payload.get("album_key"))
            if row is not None and "album_ratio" in payload:
                row.setText(3, f"{float(payload['album_ratio']):.0%}")

    @Slot(int)
    def show_rate_limit(self, seconds: int):
        self.task_status.setText(f"Telegram 限流，{seconds} 秒后重试" if seconds else "上传中")
        self._show_queue_cooldown(seconds or None)

    def _show_queue_cooldown(self, seconds):
        row = self._queue_rows.get(self._active_task_id)
        if row is not None:
            status = "上传中" if seconds is None else f"冷却中 · {seconds} 秒"
            row.setText(1, status)
            row.setData(1, Qt.ItemDataRole.UserRole, status)
            row.setForeground(1, QColor(status_color(status)))
            row.setToolTip(1, status)

    @Slot(object)
    def show_preliminary_progress(self, event):
        size = self._size_formatter
        self.preliminary_metrics.setText(
            f"预上传 {size(event.uploaded_bytes)} / {size(event.total_bytes)}"
            f" · {size(event.bytes_per_second)}/s"
        )
        self.preliminary_metrics.show()

    def finish_session(self, success: bool, message: str):
        self.preliminary_metrics.hide()
        self.stop_button.setEnabled(False)
        self.immediate_stop_button.setEnabled(False)
        self.attempt_label.hide()
        self.task_status.setText("已完成" if success else message)
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
        if self._displayed_task is not None and self.album_files.count() == 1:
            first = self.album_files.item(0)
            if first is not None and not first.flags():
                first.setForeground(QColor(status_color("MUTED")))


__all__ = ["TaskPage", "format_eta", "format_size", "kind_label"]
