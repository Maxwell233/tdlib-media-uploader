# -*- coding: utf-8 -*-
"""Independent caption editing dialog with real-time character limit countdown."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from ...core.album import compose_caption, with_filename_description
from ..theme import THEME


class CaptionEditDialog(QDialog):
    """Modern modal for editing and previewing album caption templates."""

    def __init__(
        self,
        base_label: str,
        custom_text: str,
        separator: str = " · ",
        items: list | None = None,
        *,
        kind: str = "video",
        caption_limit: int = 4096,
        include_filenames: bool = False,
        include_filename_numbers: bool = True,
        include_base: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("编辑媒体组标题")
        self.setMinimumWidth(600)
        self.resize(640, 420)

        self.kind = kind
        self.separator = separator
        self.items = items or []
        self.caption_limit = caption_limit
        self.include_filenames = include_filenames
        self.include_filename_numbers = include_filename_numbers
        self.include_base = include_base

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self.base_edit = QLineEdit(base_label)
        self.base_edit.setReadOnly(kind == "image")
        form.addRow("基础组标题", self.base_edit)

        self.custom_edit = QPlainTextEdit(custom_text)
        self.custom_edit.setPlaceholderText("可输入多行追加文字；留空表示仅使用基础标题")
        self.custom_edit.setMaximumHeight(90)
        form.addRow("自定义追加", self.custom_edit)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        form.addRow("最终发送预览", self.preview)

        layout.addLayout(form)

        # Status & character counter
        self.caption_count = QLabel()
        self.caption_count.setObjectName("mutedLabel")
        layout.addWidget(self.caption_count)

        caption_hint = QLabel(
            f"编辑器使用 {caption_limit} 字符软上限；连接 Telegram 后将根据当前账号与会员特权最终确认。"
        )
        caption_hint.setObjectName("mutedLabel")
        caption_hint.setWordWrap(True)
        layout.addWidget(caption_hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.base_edit.textChanged.connect(self._update_preview)
        self.custom_edit.textChanged.connect(self._update_preview)
        self._update_preview()

    def _update_preview(self):
        caption = compose_caption(
            self.base_edit.text() if self.include_base else "",
            self.custom_edit.toPlainText(),
            self.separator,
        )
        try:
            rendered = with_filename_description(
                caption,
                self.items,
                self.include_filenames,
                self.include_filename_numbers,
                max_chars=self.caption_limit,
            )
            self.preview.setPlainText(rendered)
            chars = len(rendered)
            self.caption_count.setText(
                f"标题长度 {len(caption)}/{self.caption_limit} 字符 · 发送预览 {chars}/{self.caption_limit} 字符"
            )
            if chars > self.caption_limit:
                self.caption_count.setStyleSheet(f"color: {THEME.danger};")
            else:
                self.caption_count.setStyleSheet(f"color: {THEME.text_muted};")
        except Exception as exc:
            self.preview.setPlainText(str(exc))
            self.caption_count.setText(f"标题格式错误：{exc}")
            self.caption_count.setStyleSheet(f"color: {THEME.danger};")

    @property
    def custom_text(self) -> str:
        return self.custom_edit.toPlainText()

    @property
    def base_text(self) -> str:
        return self.base_edit.text()


__all__ = ["CaptionEditDialog"]
