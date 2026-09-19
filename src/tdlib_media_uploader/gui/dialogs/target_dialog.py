# -*- coding: utf-8 -*-
"""Target configuration dialog for TDLib Media Uploader."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ...config.paths import read_version
from ..components.telegram_target_editor import TelegramTargetEditor
from ..config_service import get_cfg as _cfg, write_config_values as _write_config_values
from ..tools import kind_label as _kind_label, require_kind as _require_kind


class TargetDialog(QDialog):
    """Lightweight dialog editing ONLY the Telegram destination target for the selected media kind."""

    def __init__(self, kind="video", parent=None):
        if not isinstance(kind, str):
            parent = kind if parent is None else parent
            kind = "video"
        kind = _require_kind(kind)
        super().__init__(parent)
        self.kind = kind
        accent = _kind_label(self.kind)
        version = read_version()
        self.setWindowTitle(f"修改{accent}上传目标 · V{version}")
        self.setMinimumWidth(480)
        self.resize(520, 280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        # 1. Target editor component
        self.target_editor = TelegramTargetEditor(self.kind, self)
        layout.addWidget(self.target_editor)

        # 2. Informative hint
        hint = QLabel(
            "超级群组和频道的 Chat ID 通常以 -100 开头；频道不需要也不支持 Forum Topic。\n"
            "相册大小、排序方式与文件名格式等媒体参数请前往“设置 -> 上传参数”中调整。"
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch(1)

        # 3. Action buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Hidden media container for backward compatibility with legacy tests
        self.media_box = QWidget(self)
        self.media_box.setVisible(False)
        self.video_filename_numbers = QCheckBox(self.media_box)
        self.video_filename_numbers.setChecked(bool(_cfg("VIDEO_CAPTION_INCLUDE_FILENAME_NUMBERS", True)))
        self.mixed_filename_numbers = QCheckBox(self.media_box)
        self.mixed_filename_numbers.setChecked(bool(_cfg("MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS", True)))

    @property
    def target_mode(self):
        return self.target_editor.target_mode

    @property
    def chat_id(self):
        return self.target_editor.chat_id

    @property
    def channel_chat_id(self):
        return self.target_editor.channel_chat_id

    @property
    def topic_id(self):
        return self.target_editor.topic_id

    def _save(self):
        valid, msg = self.target_editor.validate()
        if not valid:
            QMessageBox.critical(self, "保存失败", msg)
            return

        values = self.target_editor.collect_values(self.kind)
        error = _write_config_values(values)
        if error:
            QMessageBox.critical(self, "保存失败", error)
            return
        self.accept()


__all__ = ["TargetDialog"]
