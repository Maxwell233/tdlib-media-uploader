# -*- coding: utf-8 -*-
"""Modern overview dashboard page for TDLib Media Uploader Beta 3."""

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

from ...config.paths import read_version
from ..components.cards import ActionCard, StatCard
from ..theme import THEME


def format_size(value: float | int | None) -> str:
    """Format a byte count with appropriate binary unit."""
    value = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def _card(title: str, value: str = "—", subtitle: str = "") -> tuple[QFrame, QLabel]:
    card = StatCard(title, value, subtitle)
    return card, card.value_label


class HomePage(QWidget):
    """Modern dashboard rendering application state and providing quick task routes."""

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

        # Hero Banner
        header = QVBoxLayout()
        header.setSpacing(4)
        heading = QLabel("概览工作台")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("本地媒体文件自动化编排 → Telegram 超级群组 Topic / Channel 频道")
        subtitle.setObjectName("mutedLabel")
        header.addWidget(heading)
        header.addWidget(subtitle)
        layout.addLayout(header)

        # Key Status Cards
        stats_layout = QGridLayout()
        stats_layout.setSpacing(12)

        self.connection_card, self.connection_value = _card(
            "Telegram 连接", "未连接", "TDLib 原生客户端状态"
        )
        self.task_card, self.task_value = _card(
            "当前运行任务", "无", "后台上传进度"
        )
        self.today_card, self.today_value = _card(
            "本次扫描统计", "—", "最近一次媒体扫描"
        )

        stats_layout.addWidget(self.connection_card, 0, 0)
        stats_layout.addWidget(self.task_card, 0, 1)
        stats_layout.addWidget(self.today_card, 0, 2)
        layout.addLayout(stats_layout)

        # Quick Launch Section with Action Cards
        action_box = QGroupBox("快速开始")
        action_layout = QGridLayout(action_box)
        action_layout.setSpacing(12)

        self.video_card = ActionCard(
            "视频上传",
            "自动按拍摄月份或固定数量智能分发到多个 Album 媒体组；支持 EXIF 与封面提取。",
            icon_text="🎬",
            badge_text="最大 4 GB",
        )
        self.video_card.clicked.connect(lambda: self.start_upload.emit("video"))

        self.image_card = ActionCard(
            "图片上传",
            "按自然顺序或修改时间分批分组；支持超限自动高质量压缩，确保顺利发送。",
            icon_text="🖼",
            badge_text="最大 10 MB",
        )
        self.image_card.clicked.connect(lambda: self.start_upload.emit("image"))

        self.mixed_card = ActionCard(
            "混合上传",
            "以一级子文件夹为分组单元，同组内照片与视频严格保序混合成 Album 发送。",
            icon_text="📦",
            badge_text="图片 + 视频",
        )
        self.mixed_card.clicked.connect(lambda: self.start_upload.emit("mixed"))

        self.settings_card = ActionCard(
            "系统配置",
            "配置 Telegram API ID/Hash、SOCKS5/HTTP 代理、SMB 暂存目录及扫描并发参数。",
            icon_text="⚙",
            badge_text="设置与诊断",
        )
        self.settings_card.clicked.connect(self.open_settings)

        action_layout.addWidget(self.video_card, 0, 0)
        action_layout.addWidget(self.image_card, 0, 1)
        action_layout.addWidget(self.mixed_card, 1, 0)
        action_layout.addWidget(self.settings_card, 1, 1)
        layout.addWidget(action_box)

        # Workflow Notes & Tips
        note_box = QGroupBox(f"V{version} 运行与操作规范")
        note_layout = QVBoxLayout(note_box)
        note_layout.setSpacing(6)
        note_body = QLabel(
            "• 首次启动请先进入【设置与诊断】填写 Telegram API 凭据，必要时配置代理服务器。\n"
            "• 在左侧选择对应上传类型，选择本地来源目录并点击【扫描目录】。\n"
            "• 扫描完成后可在预览树中展开检查 Album 分组，双击条目可快速自定义媒体组标题与文件名格式。\n"
            "• 上传期间支持【安全停止】（在当前 Album 发送完毕后优雅退出）与【立即中断】。\n"
            "• 所有发送过程均受断点记录保护，再次扫描将自动跳过已确认发送的文件。"
        )
        note_body.setObjectName("mutedLabel")
        note_body.setWordWrap(True)
        note_layout.addWidget(note_body)
        layout.addWidget(note_box)

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
