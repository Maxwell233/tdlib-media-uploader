# -*- coding: utf-8 -*-
"""Modern sidebar navigation component for Beta 3."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...config.paths import read_version
from ..theme import THEME, build_stylesheet, get_current_theme_mode, toggle_theme


def _format_badge(version: str) -> str:
    v = str(version or "").strip()
    if "beta" in v.lower():
        parts = v.lower().split("beta")
        suffix = parts[-1].strip(" -_.")
        return f"Beta {suffix}" if suffix else "Beta"
    return "Beta 3"


class NavigationSidebar(QFrame):
    """Sidebar navigation panel containing branding, grouped routes, and status."""

    item_selected = Signal(int)

    NAV_ITEMS = (
        ("概览", "dashboard"),
        ("视频上传", "video"),
        ("图片上传", "image"),
        ("混合上传", "mixed"),
        ("未确认上传", "inflight"),
        ("任务中心", "task"),
        ("历史记录", "history"),
        ("设置与诊断", "settings"),
    )

    def __init__(self, version: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebarFrame")
        self.setFixedWidth(230)
        self.version = str(version or read_version()).strip()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Brand header
        brand_widget = QWidget()
        brand_layout = QVBoxLayout(brand_widget)
        brand_layout.setContentsMargins(18, 20, 18, 14)
        brand_layout.setSpacing(4)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)

        app_title = QLabel("TDLib Uploader")
        app_title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {THEME.text_primary};")
        title_row.addWidget(app_title)

        badge = QLabel(_format_badge(self.version))
        badge.setStyleSheet(
            f"background-color: {THEME.bg_selection}; color: {THEME.accent_hover}; "
            f"font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 6px;"
        )
        title_row.addWidget(badge)
        title_row.addStretch(1)

        # Theme toggle button (Sun / Moon)
        curr_mode = get_current_theme_mode()
        self.theme_btn = QPushButton("🌙" if curr_mode == "dark" else "☀️")
        self.theme_btn.setObjectName("themeToggleBtn")
        self.theme_btn.setFixedSize(28, 28)
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.setToolTip("切换明暗主题 (☀️/🌙)")
        self.theme_btn.clicked.connect(self._toggle_theme)
        title_row.addWidget(self.theme_btn)

        brand_layout.addLayout(title_row)

        subtitle = QLabel("TG 媒体批量上传工具")
        subtitle.setStyleSheet(f"font-size: 11px; color: {THEME.text_dim};")
        brand_layout.addWidget(subtitle)
        layout.addWidget(brand_widget)

        # Nav list
        self.list = QListWidget()
        self.list.setObjectName("sidebar")
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        for label, _key in self.NAV_ITEMS:
            item = QListWidgetItem(label)
            self.list.addItem(item)

        self.list.currentRowChanged.connect(self.item_selected.emit)
        layout.addWidget(self.list, 1)

        # Bottom status card
        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(14, 12, 14, 14)
        footer_layout.setSpacing(4)

        status_box = QFrame()
        status_box.setStyleSheet(
            f"background-color: {THEME.bg_surface}; border: 1px solid {THEME.border_subtle}; "
            f"border-radius: 8px; padding: 6px 10px;"
        )
        box_layout = QHBoxLayout(status_box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(8)

        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {THEME.text_dim}; font-size: 10px;")
        self.status_text = QLabel("TG 未连接")
        self.status_text.setStyleSheet(f"color: {THEME.text_muted}; font-size: 11px; font-weight: 500;")

        box_layout.addWidget(self.status_dot)
        box_layout.addWidget(self.status_text, 1)
        footer_layout.addWidget(status_box)

        version_label = QLabel(f"Version {self.version}")
        version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version_label.setStyleSheet(f"color: {THEME.text_dim}; font-size: 10px;")
        footer_layout.addWidget(version_label)

        layout.addWidget(footer)

    def _toggle_theme(self):
        new_mode = toggle_theme()
        self.theme_btn.setText("☀️" if new_mode == "light" else "🌙")
        self.theme_btn.setToolTip(f"当前{'浅色' if new_mode == 'light' else '深色'}模式，点击切换主题 (☀️/🌙)")
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(THEME))

    def set_connection_status(self, connected: bool, text: str = ""):
        color = THEME.success if connected else THEME.warning if text else THEME.text_dim
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 10px;")
        self.status_text.setText(text or ("TG 已连接" if connected else "TG 未连接"))

    def setCurrentRow(self, row: int):
        self.list.setCurrentRow(row)

    def currentRow(self) -> int:
        return self.list.currentRow()


__all__ = ["NavigationSidebar"]
