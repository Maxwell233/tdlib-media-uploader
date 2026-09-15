"""Package-owned overview page for the migration-period desktop GUI."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from runtime_paths import read_version


def format_size(value: float | int | None) -> str:
    """Format a byte count without depending on the root GUI module."""

    value = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


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
    """Render application status and route users to a media page."""

    start_upload = Signal(str)
    open_settings = Signal()

    def __init__(
        self,
        *,
        app_version: str | None = None,
        size_formatter: Callable[[float | int | None], str] | None = None,
    ):
        super().__init__()
        self._size_formatter = size_formatter or format_size
        version = app_version or read_version()
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

        note = QGroupBox(f"V{version} 运行提示")
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
            f"{result['total_files']} 个文件 · {self._size_formatter(result['total_bytes'])}"
        )

    def clear_scan(self):
        self.today_value.setText("—")

    def set_connection(self, text: str, good: bool = False):
        self.connection_value.setText(text)
        self.connection_value.setProperty("good", good)
        self.connection_value.style().unpolish(self.connection_value)
        self.connection_value.style().polish(self.connection_value)


__all__ = ["HomePage", "format_size"]
