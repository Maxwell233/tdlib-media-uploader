# -*- coding: utf-8 -*-
"""Modern overview dashboard page focusing on media upload workflows for Beta 4."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...config.paths import read_version
from ..components.cards import ActionCard, StatCard
from ..icons import get_svg_pixmap
from ..theme import THEME
from ..tools import format_size


def _card(title: str, value: str = "—", subtitle: str = "") -> tuple[StatCard, QLabel]:
    card = StatCard(title, value, subtitle)
    return card, card.value_label


class HomePage(QWidget):
    """Modern dashboard rendering application state with immediate upload entrypoints."""

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
        layout.setSpacing(20)

        # 1. Header with branding and subtle version badge
        header = QVBoxLayout()
        header.setSpacing(4)
        title_row = QHBoxLayout()
        heading = QLabel("TDLib Media Uploader")
        heading.setObjectName("pageTitle")
        title_row.addWidget(heading)
        title_row.addStretch(1)
        header.addLayout(title_row)

        subtitle = QLabel("本地媒体文件自动化编排 → Telegram 超级群组 Topic / Channel 频道")
        subtitle.setObjectName("mutedLabel")
        header.addWidget(subtitle)
        layout.addLayout(header)

        # 2. Primary Focus: Upload Media Action Cards (Video, Image, Mixed)
        launch_section = QVBoxLayout()
        launch_section.setSpacing(10)
        launch_title = QLabel("开始上传")
        launch_title.setObjectName("sectionTitle")
        launch_section.addWidget(launch_title)

        action_layout = QGridLayout()
        action_layout.setSpacing(14)

        self.video_card = ActionCard(
            "视频上传",
            "按拍摄月份或固定数量智能分批为 Album，支持 EXIF 日期解析与缩略图提取。",
            icon_name="video",
            badge_text="最大 4 GB",
        )
        self.video_card.clicked.connect(lambda: self.start_upload.emit("video"))

        self.image_card = ActionCard(
            "图片上传",
            "按自然顺序或修改时间分批打包，超限自动高质量压缩，保持原生画质。",
            icon_name="image",
            badge_text="最大 10 MB",
        )
        self.image_card.clicked.connect(lambda: self.start_upload.emit("image"))

        self.mixed_card = ActionCard(
            "混合上传",
            "以同级文件夹为单元，同组内照片与视频严格保序混编发送为 Album。",
            icon_name="mixed",
            badge_text="图片 + 视频",
        )
        self.mixed_card.clicked.connect(lambda: self.start_upload.emit("mixed"))

        action_layout.addWidget(self.video_card, 0, 0)
        action_layout.addWidget(self.image_card, 0, 1)
        action_layout.addWidget(self.mixed_card, 0, 2)
        launch_section.addLayout(action_layout)
        layout.addLayout(launch_section)

        # 3. Current Task Card (Active progress or clean minimal empty state)
        task_section = QVBoxLayout()
        task_section.setSpacing(8)
        task_header = QLabel("当前运行任务")
        task_header.setObjectName("sectionTitle")
        task_section.addWidget(task_header)

        self.task_card = QFrame()
        self.task_card.setObjectName("surfaceCard")
        task_box_layout = QHBoxLayout(self.task_card)
        task_box_layout.setContentsMargins(18, 14, 18, 14)
        task_box_layout.setSpacing(14)

        self.task_icon_label = QLabel()
        self.task_icon_label.setPixmap(get_svg_pixmap("task", color=THEME.text_muted, size=22))
        task_box_layout.addWidget(self.task_icon_label)

        task_info_layout = QVBoxLayout()
        task_info_layout.setSpacing(3)
        self.task_value = QLabel("无正在运行的上传任务")
        self.task_value.setObjectName("valueLabel")
        self.task_hint = QLabel("在上方选择媒体类型即可进入上传工作台")
        self.task_hint.setObjectName("mutedLabel")
        task_info_layout.addWidget(self.task_value)
        task_info_layout.addWidget(self.task_hint)
        task_box_layout.addLayout(task_info_layout, 1)

        task_section.addWidget(self.task_card)
        layout.addLayout(task_section)

        # 4. Status and Activity Row (Telegram Connection & Recent Scan)
        status_row = QHBoxLayout()
        status_row.setSpacing(14)

        self.connection_card, self.connection_value = _card(
            "Telegram 连接状态", "未连接", "TDLib 原生会话"
        )
        self.today_card, self.today_value = _card(
            "本次扫描统计", "—", "最近一次媒体扫描"
        )

        status_row.addWidget(self.connection_card, 1)
        status_row.addWidget(self.today_card, 1)
        layout.addLayout(status_row)

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

    def set_task_running(self, kind: str, summary: str = ""):
        from ..tools import kind_label
        label = kind_label(kind)
        self.task_value.setText(f"{label}上传中")
        self.task_hint.setText(summary or "正在向 Telegram 发送媒体，请在任务中心查看详情")

    def set_task_progress(self, current: int, total: int, speed: str = "", eta: str = ""):
        parts = [f"{current} / {total} 个文件"]
        if speed:
            parts.append(speed)
        if eta:
            parts.append(f"ETA {eta}")
        self.task_hint.setText(" · ".join(parts))

    def set_task_idle(self):
        self.task_value.setText("无正在运行的上传任务")
        self.task_hint.setText("在上方选择媒体类型即可进入上传工作台")

    def refresh_theme(self):
        """Re-polish connection and dynamic values on theme change."""
        self.connection_value.style().unpolish(self.connection_value)
        self.connection_value.style().polish(self.connection_value)
        self.task_icon_label.setPixmap(get_svg_pixmap("task", color=THEME.text_muted, size=22))
        for card in (self.video_card, self.image_card, self.mixed_card):
            if hasattr(card, "refresh_theme"):
                card.refresh_theme()


__all__ = ["HomePage", "format_size"]
