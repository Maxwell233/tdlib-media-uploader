# -*- coding: utf-8 -*-
"""Base class and common helpers for inline settings panels."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLineEdit,
    QSpinBox,
    QAbstractSpinBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class SettingsPanel(QWidget):
    """Abstract base class for inline configuration panels."""

    changed = Signal()
    dirty_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dirty = False

    def load(self):
        """Load current configuration into panel inputs."""
        raise NotImplementedError

    def validate(self) -> tuple[bool, str]:
        """Validate input values. Return (True, '') if valid, (False, 'error') otherwise."""
        return True, ""

    def collect_values(self) -> dict:
        """Collect configuration dictionary mapping (section, key) -> value."""
        raise NotImplementedError

    def input_snapshot(self):
        """Capture editable controls without labels or internal spinbox editors."""
        values = {}
        for widget in self.findChildren(QWidget):
            if isinstance(widget, QCheckBox):
                values[widget] = ("setChecked", widget.isChecked())
            elif isinstance(widget, QComboBox):
                values[widget] = ("setCurrentIndex", widget.currentIndex())
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                values[widget] = ("setValue", widget.value())
            elif isinstance(widget, QLineEdit) and not isinstance(
                widget.parentWidget(), (QAbstractSpinBox, QComboBox)
            ):
                values[widget] = ("setText", widget.text())
        return values

    def reset(self):
        """Reset panel inputs to loaded configuration."""
        self.load()
        self.mark_clean()

    def set_dirty(self, dirty: bool = True):
        if self._dirty != dirty:
            self._dirty = dirty
            self.dirty_changed.emit(dirty)
        self.changed.emit()

    def mark_clean(self):
        if self._dirty:
            self._dirty = False
            self.dirty_changed.emit(False)

    def is_dirty(self) -> bool:
        return self._dirty

    @staticmethod
    def create_card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("surfaceCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        if title:
            header = QLabel(title)
            header.setObjectName("cardTitle")
            layout.addWidget(header)
        return card, layout


__all__ = ["SettingsPanel"]
