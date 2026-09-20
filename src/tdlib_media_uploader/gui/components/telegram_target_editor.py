# -*- coding: utf-8 -*-
"""Reusable Telegram Target Editor component."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QWidget,
)

from ..config_service import target_for as _target_for


class TelegramTargetEditor(QWidget):
    """Component for selecting Telegram target mode (Forum Topic / Channel) and IDs."""

    changed = Signal()

    def __init__(self, kind: str = "video", parent=None):
        super().__init__(parent)
        self.kind = kind
        self._initial_state: dict[str, str | int] = {}
        self._dirty = False

        form = QFormLayout(self)
        self.form = form
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(10)

        self.target_mode = QComboBox()
        self.target_mode.addItem("超级群组 Forum Topic", "forum_topic")
        self.target_mode.addItem("Channel 频道", "channel")
        form.addRow("目标类型", self.target_mode)

        self.chat_id = QLineEdit()
        self.chat_id.setPlaceholderText("例如 -1001234567890")
        self.channel_chat_id = QLineEdit()
        self.channel_chat_id.setPlaceholderText("例如 -1001234567890")
        self.topic_id = QLineEdit()
        self.topic_id.setPlaceholderText("例如 1 或 42")

        form.addRow("群组 Chat ID", self.chat_id)
        form.addRow("频道 Chat ID", self.channel_chat_id)
        form.addRow("Forum Topic ID", self.topic_id)

        self.target_mode.currentIndexChanged.connect(self._on_field_changed)
        self.chat_id.textChanged.connect(self._on_field_changed)
        self.channel_chat_id.textChanged.connect(self._on_field_changed)
        self.topic_id.textChanged.connect(self._on_field_changed)

        self.load(self.kind)

    def load(self, kind: str | None = None):
        if kind:
            self.kind = kind
        target = _target_for(self.kind)
        mode = str(target.get("target_mode", "forum_topic") or "forum_topic")
        group_id = str(target.get("group_chat_id", 0) or target.get("chat_id", 0) or "")
        channel_id = str(target.get("channel_chat_id", 0) or "")
        topic_id = str(target.get("forum_topic_id", 0) or "")

        self.blockSignals(True)
        idx = self.target_mode.findData(mode)
        self.target_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.chat_id.setText(group_id)
        self.channel_chat_id.setText(channel_id)
        self.topic_id.setText(topic_id)
        self.blockSignals(False)

        self._initial_state = {
            "mode": mode,
            "chat_id": group_id,
            "channel_chat_id": channel_id,
            "topic_id": topic_id,
        }
        self._dirty = False
        self._update_visibility()

    def _update_visibility(self):
        channel = self.target_mode.currentData() == "channel"
        for field, visible in (
            (self.chat_id, not channel),
            (self.channel_chat_id, channel),
            (self.topic_id, not channel),
        ):
            field.setVisible(visible)
            label = self.form.labelForField(field)
            if label is not None:
                label.setVisible(visible)

    def _on_field_changed(self):
        self._update_visibility()
        current = {
            "mode": self.target_mode.currentData(),
            "chat_id": self.chat_id.text().strip(),
            "channel_chat_id": self.channel_chat_id.text().strip(),
            "topic_id": self.topic_id.text().strip(),
        }
        self._dirty = current != self._initial_state
        self.changed.emit()

    def is_dirty(self) -> bool:
        return self._dirty

    def mark_clean(self):
        self._initial_state = {
            "mode": self.target_mode.currentData(),
            "chat_id": self.chat_id.text().strip(),
            "channel_chat_id": self.channel_chat_id.text().strip(),
            "topic_id": self.topic_id.text().strip(),
        }
        self._dirty = False

    def reset(self):
        self.load(self.kind)

    def validate(self) -> tuple[bool, str]:
        try:
            group_id = int(self.chat_id.text().strip() or "0")
            channel_id = int(self.channel_chat_id.text().strip() or "0")
            topic_id = int(self.topic_id.text().strip() or "0")
        except ValueError:
            return False, "Chat ID 和 Topic ID 都必须是整数。"

        mode = self.target_mode.currentData() or "forum_topic"
        if mode == "channel":
            if channel_id == 0:
                return False, "频道 Chat ID 不能为 0。"
        else:
            if group_id == 0:
                return False, "群组 Chat ID 不能为 0。"
            if topic_id <= 0:
                return False, "Forum Topic ID 必须大于 0。"
        return True, ""

    def collect_values(self, kind: str | None = None) -> dict:
        def integer_or_text(field):
            text = field.text().strip()
            try:
                return int(text or "0")
            except ValueError:
                return text

        k = kind or self.kind
        mode = self.target_mode.currentData() or "forum_topic"
        group_id = integer_or_text(self.chat_id)
        channel_id = integer_or_text(self.channel_chat_id)
        topic_id = integer_or_text(self.topic_id)
        return {
            (f"telegram.{k}", "target_mode"): mode,
            (f"telegram.{k}", "chat_id"): group_id,
            (f"telegram.{k}", "channel_chat_id"): channel_id,
            (f"telegram.{k}", "forum_topic_id"): topic_id,
        }


__all__ = ["TelegramTargetEditor"]
