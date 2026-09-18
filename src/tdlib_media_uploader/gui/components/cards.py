# -*- coding: utf-8 -*-
"""Modern metric cards and interactive action cards for Beta 3."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from ..theme import THEME


class StatCard(QFrame):
    """Modern elevated card displaying a key metric or status."""

    def __init__(
        self,
        title: str,
        value: str = "—",
        subtitle: str = "",
        *,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("mutedLabel")
        top_row.addWidget(self.title_label)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        self.value_label = QLabel(value)
        self.value_label.setObjectName("statValue")
        layout.addWidget(self.value_label)

        if subtitle:
            self.subtitle_label = QLabel(subtitle)
            self.subtitle_label.setObjectName("mutedLabel")
            layout.addWidget(self.subtitle_label)
        else:
            self.subtitle_label = None

    def set_value(self, value: str, good: bool | None = None):
        self.value_label.setText(str(value))
        if good is not None:
            self.value_label.setProperty("good", good)
            self.value_label.style().unpolish(self.value_label)
            self.value_label.style().polish(self.value_label)

    def set_subtitle(self, text: str):
        if self.subtitle_label is None:
            self.subtitle_label = QLabel(text)
            self.subtitle_label.setObjectName("mutedLabel")
            self.layout().addWidget(self.subtitle_label)
        else:
            self.subtitle_label.setText(text)

    def set_description(self, text: str):
        self.set_subtitle(text)


class ActionCard(QFrame):
    """Interactive card for fast navigation or initiating an action."""

    clicked = Signal()

    def __init__(
        self,
        title: str,
        description: str,
        icon_text: str = "",
        badge_text: str = "",
        *,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)

        header = QHBoxLayout()
        if icon_text:
            icon_label = QLabel(icon_text)
            icon_label.setStyleSheet(f"font-size: 18px; color: {THEME.accent};")
            header.addWidget(icon_label)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("sectionTitle")
        header.addWidget(self.title_label)
        header.addStretch(1)
        if badge_text:
            badge = QLabel(badge_text)
            badge.setObjectName("badgeLabel")
            badge.setStyleSheet(
                f"background-color: {THEME.bg_selection}; color: {THEME.accent_hover};"
            )
            header.addWidget(badge)
        layout.addLayout(header)

        self.desc_label = QLabel(description)
        self.desc_label.setObjectName("mutedLabel")
        self.desc_label.setWordWrap(True)
        layout.addWidget(self.desc_label)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


__all__ = ["ActionCard", "StatCard"]
