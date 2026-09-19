# -*- coding: utf-8 -*-
"""Modern task center page for TDLib Media Uploader Beta 4."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..theme import THEME
from ..tools import format_eta, format_size, kind_label


class TaskPage(QWidget):
    """Mission Control center displaying real-time upload progress, speed and diagnostics."""

    stop_requested = Signal()
    safe_stop_requested = Signal()
    immediate_stop_requested = Signal()

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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

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

        # 1. Dual Progress & Real-time Metrics Card (No QGroupBox)
        progress_card = QFrame()
        progress_card.setObjectName("surfaceCard")
        progress_layout = QVBoxLayout(progress_card)
        progress_layout.setContentsMargins(18, 16, 18, 16)
        progress_layout.setSpacing(10)

        # Album title and progress bar
        self.album_label = QLabel("当前 Album：尚未开始")
        self.album_label.setObjectName("sectionTitle")
        progress_layout.addWidget(self.album_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        progress_layout.addWidget(self.progress)

        # Metrics bar
        self.metrics = QLabel("速度 — · Mbps — · ETA --:-- · 文件 0/0 · 已传 0 B")
        self.metrics.setObjectName("mutedLabel")
        progress_layout.addWidget(self.metrics)

        layout.addWidget(progress_card)

        # 2. Split Area: Current Album Files + Live Upload Log Terminal
        split = QHBoxLayout()
        split.setSpacing(14)

        # Current Album files panel
        album_card = QFrame()
        album_card.setObjectName("surfaceCard")
        album_layout = QVBoxLayout(album_card)
        album_layout.setContentsMargins(14, 12, 14, 12)
        album_layout.setSpacing(8)

        album_title = QLabel("当前 Album 包含的文件")
        album_title.setObjectName("sectionTitle")
        album_layout.addWidget(album_title)

        self.album_files = QListWidget()
        self.album_files.setAlternatingRowColors(True)
        album_layout.addWidget(self.album_files, 1)
        split.addWidget(album_card, 1)

        # Live terminal log panel
        log_card = QFrame()
        log_card.setObjectName("surfaceCard")
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(14, 12, 14, 12)
        log_layout.setSpacing(8)

        log_header = QHBoxLayout()
        log_title = QLabel("实时上传日志")
        log_title.setObjectName("sectionTitle")
        log_header.addWidget(log_title)
        log_header.addStretch(1)

        self.clear_log_btn = QPushButton("清空日志")
        self.clear_log_btn.setObjectName("ghostButton")
        self.clear_log_btn.clicked.connect(lambda: self.log.clear())
        log_header.addWidget(self.clear_log_btn)
        log_layout.addLayout(log_header)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        log_layout.addWidget(self.log, 1)
        split.addWidget(log_card, 2)

        layout.addLayout(split, 1)

        # 3. Bottom Action Bar (Semantic secondary vs danger button)
        bottom = QHBoxLayout()
        bottom.setSpacing(12)
        bottom.addStretch(1)

        self.stop_button = QPushButton("安全停止（当前 Album 发送完毕后退出）")
        self.stop_button.setObjectName("secondaryButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._emit_safe_stop)

        self.immediate_stop_button = QPushButton("立即中断")
        self.immediate_stop_button.setObjectName("dangerButton")
        self.immediate_stop_button.setEnabled(False)
        self.immediate_stop_button.clicked.connect(self.immediate_stop_requested)

        bottom.addWidget(self.stop_button)
        bottom.addWidget(self.immediate_stop_button)
        layout.addLayout(bottom)

    def _emit_safe_stop(self):
        self.safe_stop_requested.emit()
        self.stop_requested.emit()

    def start_session(self, kind: str, result: dict):
        self.title.setText(f"任务中心 · {kind_label(kind)}")
        self.task_status.setText("正在启动…")
        self.stop_button.setEnabled(True)
        self.immediate_stop_button.setEnabled(True)
        self.progress.setValue(0)
        self.metrics.setText(
            f"待上传 {result['pending_files']} 个 · "
            f"{self._size_formatter(result['pending_bytes'])} · "
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
            f"速度 {self._size_formatter(speed)}/s · {mbps:,.1f} Mbps · "
            f"ETA {self._eta_formatter(payload.get('eta'))} · "
            f"Album {payload.get('album_number', 0)}/{payload.get('album_total', 0)} · "
            f"文件 {payload.get('done_files', 0)}/{payload.get('total_files', 0)} · "
            f"已传 {self._size_formatter(payload.get('done_bytes', 0))} / "
            f"{self._size_formatter(payload.get('total_bytes', 0))}"
        )

    def finish_session(self, success: bool, message: str):
        self.stop_button.setEnabled(False)
        self.immediate_stop_button.setEnabled(False)
        self.task_status.setText("已完成" if success else message)
        self.log.appendPlainText(("✓ " if success else "! ") + message)

    def refresh_theme(self):
        """Re-apply dynamic theme styling."""
        pass


__all__ = ["TaskPage", "format_eta", "format_size", "kind_label"]
