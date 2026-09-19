# -*- coding: utf-8 -*-
"""Status pill component for displaying state tags with distinct colors."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from ..theme import THEME


def _current_styles():
    return {
        "success": (THEME.success, THEME.success_bg),
        "warning": (THEME.warning, THEME.warning_bg),
        "danger": (THEME.danger, THEME.danger_bg),
        "info": (THEME.info, THEME.info_bg),
        "neutral": (THEME.text_muted, THEME.bg_surface),
        "accent": (THEME.accent_hover, THEME.bg_selection),
    }


class StatusPill(QLabel):
    """A pill-shaped status badge with semantic color tinting."""

    STYLES = {
        "success": (THEME.success, THEME.success_bg),
        "warning": (THEME.warning, THEME.warning_bg),
        "danger": (THEME.danger, THEME.danger_bg),
        "info": (THEME.info, THEME.info_bg),
        "neutral": (THEME.text_muted, THEME.bg_surface),
        "accent": (THEME.accent_hover, THEME.bg_selection),
    }

    def __init__(self, text: str = "", status: str = "neutral", parent=None):
        super().__init__(text, parent)
        self.variant = status
        self.status = status
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(status, text)

    def set_status(self, status: str, text: str | None = None):
        self.variant = status
        self.status = status
        if text is not None:
            self.setText(text)
        styles = _current_styles()
        fg, bg = styles.get(status, styles["neutral"])
        self.setStyleSheet(
            f"""
            QLabel {{
                color: {fg};
                background-color: {bg};
                border: 1px solid {fg}40;
                border-radius: 10px;
                padding: 2px 10px;
                font-size: 11px;
                font-weight: 600;
            }}
            """
        )

    def refresh_theme(self):
        """Re-apply styling for the active status using current theme palette."""
        self.set_status(self.status)


__all__ = ["StatusPill"]
