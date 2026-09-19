# -*- coding: utf-8 -*-
"""Modern sidebar navigation component for Beta 4."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
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
from ..icons import get_svg_icon
from ..theme import THEME, build_stylesheet, get_current_theme_mode, toggle_theme


def _format_badge(version: str) -> str:
    v = str(version or "").strip()
    vl = v.lower()
    for tag in ("beta", "rc", "alpha", "dev"):
        if tag in vl:
            parts = vl.split(tag)
            suffix = parts[-1].strip(" -_.")
            tag_name = tag.upper() if tag in ("rc", "dev") else tag.capitalize()
            return f"{tag_name} {suffix}".strip()
    if v:
        return "Stable"
    return "Beta 4"


class NavigationSidebar(QFrame):
    """Sidebar navigation panel containing branding, grouped routes, and status."""

    item_selected = Signal(int)

    NAV_ITEMS = (
        ("概览", "dashboard", "dashboard"),
        ("媒体上传", "upload", "upload"),
        ("任务中心", "task", "task"),
        ("未确认上传", "inflight", "inflight"),
        ("历史记录", "history", "history"),
        ("设置与诊断", "settings", "settings"),
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
        app_title.setObjectName("sidebarAppTitle")
        title_row.addWidget(app_title)

        self.badge = QLabel(_format_badge(self.version), brand_widget)
        self.badge.setObjectName("sidebarBadge")
        self.badge.setVisible(False)
        title_row.addStretch(1)

        # Theme toggle button (Vector Sun / Moon icon)
        curr_mode = get_current_theme_mode()
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("themeToggleBtn")
        self.theme_btn.setFixedSize(30, 30)
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.setProperty("themeMode", curr_mode)
        self.theme_btn.setIcon(get_svg_icon("sun" if curr_mode == "dark" else "moon", 16, 16))
        self.theme_btn.setIconSize(QSize(16, 16))
        self.theme_btn.setToolTip("切换明暗主题 (Light / Dark)")
        self.theme_btn.clicked.connect(self._toggle_theme)
        title_row.addWidget(self.theme_btn)

        brand_layout.addLayout(title_row)

        subtitle = QLabel("TG 媒体批量上传工具")
        subtitle.setObjectName("sidebarSubtitle")
        brand_layout.addWidget(subtitle)
        layout.addWidget(brand_widget)

        # Nav list
        self.list = QListWidget()
        self.list.setObjectName("sidebar")
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setIconSize(QSize(18, 18))

        for label, _key, icon_name in self.NAV_ITEMS:
            item = QListWidgetItem(label)
            item.setIcon(get_svg_icon(icon_name, color=THEME.text_secondary, size=18, selected_color="#ffffff"))
            self.list.addItem(item)

        self.list.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self.list, 1)

        # Bottom status card
        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(14, 12, 14, 14)
        footer_layout.setSpacing(4)

        status_box = QFrame()
        status_box.setObjectName("sidebarStatusBox")
        box_layout = QHBoxLayout(status_box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(8)

        self.status_dot = QLabel("●")
        self.status_dot.setObjectName("sidebarStatusDot")
        self.status_dot.setProperty("connected", "false")
        self.status_text = QLabel("TG 未连接")
        self.status_text.setObjectName("sidebarStatusText")

        box_layout.addWidget(self.status_dot)
        box_layout.addWidget(self.status_text, 1)
        footer_layout.addWidget(status_box)

        self.version_label = QLabel(f"Version {self.version}")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label.setObjectName("sidebarVersionLabel")
        footer_layout.addWidget(self.version_label)

        layout.addWidget(footer)

    def _on_row_changed(self, row: int):
        self._update_icons()
        self.item_selected.emit(row)

    def _update_icons(self):
        curr_row = self.list.currentRow()
        for idx, (_, _, icon_name) in enumerate(self.NAV_ITEMS):
            item = self.list.item(idx)
            if item is not None:
                color = "#ffffff" if idx == curr_row else THEME.text_secondary
                item.setIcon(get_svg_icon(icon_name, color=color, size=18, selected_color="#ffffff"))

    def _toggle_theme(self):
        new_mode = toggle_theme()
        self.theme_btn.setIcon(get_svg_icon("sun" if new_mode == "dark" else "moon", 16, 16))
        self.theme_btn.setProperty("themeMode", new_mode)
        self.theme_btn.setToolTip(f"当前{'浅色' if new_mode == 'light' else '深色'}模式，点击切换主题")
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_stylesheet(THEME))
            for widget in app.topLevelWidgets():
                if hasattr(widget, "refresh_theme"):
                    widget.refresh_theme()

    def refresh_theme(self):
        """Refresh styling and re-polish dynamic elements."""
        curr_mode = get_current_theme_mode()
        self.theme_btn.setIcon(get_svg_icon("sun" if curr_mode == "dark" else "moon", 16, 16))
        self.theme_btn.setProperty("themeMode", curr_mode)
        self._update_icons()
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)

    def set_connection_status(self, connected: bool, text: str = ""):
        self.status_dot.setProperty("connected", "true" if connected else ("warn" if text else "false"))
        self.status_dot.style().unpolish(self.status_dot)
        self.status_dot.style().polish(self.status_dot)
        self.status_text.setText(text or ("TG 已连接" if connected else "TG 未连接"))

    def setCurrentRow(self, row: int):
        self.list.setCurrentRow(row)
        self._update_icons()

    def currentRow(self) -> int:
        return self.list.currentRow()


__all__ = ["NavigationSidebar"]
