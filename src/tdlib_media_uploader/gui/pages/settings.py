# -*- coding: utf-8 -*-
"""Modern inline settings page for TDLib Media Uploader."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...config.paths import CONFIG_PATH
from .. import config_service
from ..config_service import (
    get_cfg as _cfg,
    write_config_values as _write_config_values,
)
from ..icons import get_svg_icon
from ..settings import (
    AdvancedPanel,
    EnvironmentLicensePanel,
    GeneralPanel,
    StoragePanel,
    TelegramPanel,
    UploadPanel,
)


class SettingsPage(QWidget):
    """Inline settings hub with instant sub-navigation and unified persistence."""

    open_editor = Signal()
    open_scan_tools = Signal()
    clear_all_requested = Signal()
    clear_thumb_requested = Signal()
    config_saved = Signal()

    SETTINGS_CATEGORIES = (
        ("常规", "settings"),
        ("Telegram", "telegram"),
        ("上传参数", "upload"),
        ("高级选项", "sliders"),
        ("存储与缓存", "database"),
        ("环境与许可", "info"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        # Header with Title and Config Status
        header = QHBoxLayout()
        header.setSpacing(16)
        title = QLabel("设置与系统选项")
        title.setObjectName("pageTitle")
        header.addWidget(title)

        self.config_status = QLabel()
        self.config_status.setObjectName("configStatus")
        self.config_status.setWordWrap(True)
        header.addWidget(self.config_status, 1, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)

        # Main 2-level content container
        body_layout = QHBoxLayout()
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(20)

        # Left sub-navigation
        self.nav_list = QListWidget()
        self.nav_list.setObjectName("settingsNav")
        self.nav_list.setFixedWidth(160)
        self.nav_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav_list.setIconSize(QSize(16, 16))

        for label, icon_name in self.SETTINGS_CATEGORIES:
            item = QListWidgetItem(label)
            item.setIcon(get_svg_icon(icon_name, 16, 16))
            self.nav_list.addItem(item)

        body_layout.addWidget(self.nav_list)

        # Right side: stack + bottom action bar
        right_container = QWidget()
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)

        self.stack = QStackedWidget()
        self.general_panel = GeneralPanel(self)
        self.telegram_panel = TelegramPanel(self)
        self.upload_panel = UploadPanel(self)
        self.advanced_panel = AdvancedPanel(self)
        self.storage_panel = StoragePanel(self)
        self.env_panel = EnvironmentLicensePanel(self)

        self.panels = [
            self.general_panel,
            self.telegram_panel,
            self.upload_panel,
            self.advanced_panel,
            self.storage_panel,
            self.env_panel,
        ]

        for p in self.panels:
            self.stack.addWidget(p)
            p.dirty_changed.connect(self._on_dirty_changed)

        right_layout.addWidget(self.stack, 1)

        # Forward storage panel signals
        self.storage_panel.clear_all_requested.connect(self.clear_all_requested.emit)
        self.storage_panel.clear_thumb_requested.connect(self.clear_thumb_requested.emit)

        # Bottom right action bar
        actions_bar = QHBoxLayout()
        actions_bar.setContentsMargins(0, 4, 0, 0)
        actions_bar.setSpacing(10)

        self.save_status_label = QLabel()
        self.save_status_label.setObjectName("saveStatusLabel")
        actions_bar.addWidget(self.save_status_label)
        actions_bar.addStretch(1)

        self.btn_revert = QPushButton("恢复")
        self.btn_revert.setObjectName("secondaryButton")
        self.btn_revert.setEnabled(False)
        self.btn_revert.clicked.connect(self.revert_changes)
        actions_bar.addWidget(self.btn_revert)

        self.btn_save = QPushButton("保存更改")
        self.btn_save.setObjectName("primaryButton")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.save_changes)
        actions_bar.addWidget(self.btn_save)

        right_layout.addLayout(actions_bar)
        body_layout.addWidget(right_container, 1)
        layout.addLayout(body_layout, 1)

        # Connect navigation
        self.nav_list.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav_list.setCurrentRow(0)

        self.refresh()

    @property
    def cache_status(self) -> QLabel:
        return self.storage_panel.cache_status

    @property
    def env_labels(self) -> dict[str, QLabel]:
        return self.env_panel.env_labels

    def _on_dirty_changed(self, _dirty: bool):
        has_dirty = any(p.is_dirty() for p in self.panels)
        self.btn_save.setEnabled(has_dirty)
        self.btn_revert.setEnabled(has_dirty)
        if not has_dirty and self.save_status_label.property("status") == "warning":
            self.save_status_label.setText("")

    def revert_changes(self):
        """Discard uncommitted modifications in panels and reload loaded values."""
        for p in self.panels:
            p.reset()
        self._on_dirty_changed(False)
        self._set_status("已恢复未保存修改", "muted")

    def save_changes(self):
        """Validate and commit dirty configuration values to config.toml."""
        for p in self.panels:
            valid, msg = p.validate()
            if not valid:
                self._set_status(f"保存失败：{msg}", "danger")
                return

        dirty_values = {}
        for p in self.panels:
            if p.is_dirty():
                dirty_values.update(p.collect_values())

        if not dirty_values:
            self._set_status("没有检测到更改", "muted")
            return

        err = _write_config_values(dirty_values)
        if err:
            self._set_status(f"保存失败：{err}", "danger")
            return

        for p in self.panels:
            p.mark_clean()
        self._on_dirty_changed(False)
        self._set_status("✓ 设置已保存", "success")
        self.config_saved.emit()
        self.refresh()

    def _set_status(self, text: str, status_prop: str = ""):
        self.save_status_label.setText(text)
        self.save_status_label.setProperty("status", status_prop)
        self.save_status_label.style().unpolish(self.save_status_label)
        self.save_status_label.style().polish(self.save_status_label)

    def refresh_cache_stats(self):
        self.storage_panel.refresh_cache_stats()

    def refresh_theme(self):
        for idx, (_, icon_name) in enumerate(self.SETTINGS_CATEGORIES):
            item = self.nav_list.item(idx)
            if item is not None:
                item.setIcon(get_svg_icon(icon_name, 16, 16))
        self.refresh()

    def refresh(self, update_cache: bool = False):
        if config_service._CONFIG_ERROR:
            self.config_status.setText(f"配置不可用：{config_service._CONFIG_ERROR}")
            self.config_status.setProperty("status", "danger")
        elif (
            _cfg("API_ID", 12345678) == 12345678
            or not str(_cfg("API_HASH", "")).strip()
            or str(_cfg("API_HASH", "")).strip() == "YOUR_API_HASH"
        ):
            self.config_status.setText("Telegram API 仍为示例值，请先配置。")
            self.config_status.setProperty("status", "warning")
        else:
            self.config_status.setText(f"配置文件已就绪：{CONFIG_PATH}")
            self.config_status.setProperty("status", "success")
        self.config_status.style().unpolish(self.config_status)
        self.config_status.style().polish(self.config_status)

        self.env_panel.load()
        if update_cache:
            self.refresh_cache_stats()

    def open_upload_parameters(self, kind: str):
        """Navigate directly to Upload Parameters category and select the specific kind subpanel."""
        self.nav_list.setCurrentRow(2)
        self.upload_panel.set_current_kind(kind)


__all__ = ["SettingsPage"]
