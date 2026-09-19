# -*- coding: utf-8 -*-
"""Status pill component for displaying state tags with distinct colors."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

class StatusPill(QLabel):
    """A pill-shaped status badge with semantic color tinting via global QSS."""

    def __init__(self, text: str = "", status: str = "neutral", parent=None):
        super().__init__(text, parent)
        self.setObjectName("statusPill")
        self.variant = status
        self.status = status
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status(status, text)

    def set_status(self, status: str, text: str | None = None):
        self.variant = status
        self.status = status
        if text is not None:
            self.setText(text)
        self.setProperty("status", status)
        self.style().unpolish(self)
        self.style().polish(self)

    def refresh_theme(self):
        """Re-polish dynamic styling for active theme palette."""
        self.style().unpolish(self)
        self.style().polish(self)


__all__ = ["StatusPill"]
