# -*- coding: utf-8 -*-
"""Modern task-center page for upload progress and diagnostics in Beta 3."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..theme import THEME

KIND_LABELS = {"video": "视频", "image": "图片", "mixed": "混合"}


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
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


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
        layout.setSpacing(14)

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

        # Dual Progress & Metrics Card
        progress_box = QGroupBox("实时上传进度")
        progress_layout = QVBoxLayout(progress_box)
        progress_layout.setSpacing(10)

        # Album title and progress bar
        album_row = QHBoxLayout()
        self.album_label = QLabel("当前 Album：尚未开始")
        self.album_label.setObjectName("valueLabel")
        album_row.addWidget(self.album_label)
        progress_layout.addLayout(album_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        progress_layout.addWidget(self.progress)

        # Metrics bar
        self.metrics = QLabel("速度 — · Mbps — · ETA --:-- · 文件 0/0 · 已传 0 B")
        self.metrics.setObjectName("mutedLabel")
        progress_layout.addWidget(self.metrics)

        layout.addWidget(progress_box)

        # Splitter: Current Album file list + Live console log
        split = QHBoxLayout()
        split.setSpacing(12)

        album_box = QGroupBox("当前 Album 包含的文件")
        album_layout = QVBoxLayout(album_box)
        self.album_files = QListWidget()
        self.album_files.setAlternatingRowColors(True)
        album_layout.addWidget(self.album_files)

        log_box = QGroupBox("实时上传日志终端")
        log_layout = QVBoxLayout(log_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        log_layout.addWidget(self.log)

        split.addWidget(album_box, 1)
        split.addWidget(log_box, 2)
        layout.addLayout(split, 1)

        # Bottom Action Bar
        bottom = QHBoxLayout()
        bottom.setSpacing(12)

        self.clear_log_btn = QPushButton("清空日志")
        self.clear_log_btn.clicked.connect(self.log.clear)
        bottom.addWidget(self.clear_log_btn)

        bottom.addStretch(1)

        self.stop_button = QPushButton("安全停止（当前 Album 发送完毕后退出）")
        self.stop_button.setObjectName("primaryButton")
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


__all__ = ["TaskPage", "format_eta", "format_size", "kind_label"]
