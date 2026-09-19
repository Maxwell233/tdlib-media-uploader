# -*- coding: utf-8 -*-
"""General settings panel for media paths and local staging."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...config.paths import STAGING_CACHE_DIR
from ..config_service import get_cfg as _cfg
from ..icons import get_svg_icon
from .base import SettingsPanel


class GeneralPanel(SettingsPanel):
    """Panel managing default media directories and local staging behaviour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initial_values = {}

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # 1. Media Directories Card
        dir_card, dir_layout = self.create_card("默认媒体目录")
        dir_form = QFormLayout()
        dir_form.setSpacing(10)

        self.video_dir = QLineEdit()
        self.image_dir = QLineEdit()
        self.mixed_dir = QLineEdit()

        def dir_row(label: str, line_edit: QLineEdit):
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(line_edit, 1)
            btn = QPushButton("选择")
            btn.setObjectName("secondaryButton")
            btn.setIcon(get_svg_icon("folder", 14, 14))
            btn.clicked.connect(lambda: self._browse_dir(line_edit, f"选择{label}"))
            row.addWidget(btn)
            dir_form.addRow(label, row)

        dir_row("视频目录", self.video_dir)
        dir_row("图片目录", self.image_dir)
        dir_row("混合目录", self.mixed_dir)

        dir_hint = QLabel("在上传工作台中点击“选择目录”也会实时更新并使用这些默认路径。")
        dir_hint.setObjectName("mutedLabel")
        dir_hint.setWordWrap(True)
        dir_layout.addLayout(dir_form)
        dir_layout.addWidget(dir_hint)
        layout.addWidget(dir_card)

        # 2. Local Staging Card
        stage_card, stage_layout = self.create_card("本地暂存（适合网络共享盘 / SMB / NAS）")
        stage_form = QFormLayout()
        stage_form.setSpacing(10)

        self.staging_mode = QComboBox()
        self.staging_mode.addItem("关闭（直接读取源文件）", "off")
        self.staging_mode.addItem("仅网络盘", "network")
        self.staging_mode.addItem("所有文件", "always")
        stage_form.addRow("暂存模式", self.staging_mode)

        self.staging_dir = QLineEdit()
        stage_dir_row = QHBoxLayout()
        stage_dir_row.setSpacing(8)
        stage_dir_row.addWidget(self.staging_dir, 1)
        browse_stage_btn = QPushButton("选择")
        browse_stage_btn.setObjectName("secondaryButton")
        browse_stage_btn.setIcon(get_svg_icon("folder", 14, 14))
        browse_stage_btn.clicked.connect(lambda: self._browse_dir(self.staging_dir, "选择暂存目录"))
        stage_dir_row.addWidget(browse_stage_btn)
        stage_form.addRow("暂存目录", stage_dir_row)

        self.staging_cleanup_on_start = QCheckBox("启动时清理过期暂存文件")
        stage_form.addRow("启动清理", self.staging_cleanup_on_start)

        self.staging_cleanup_days = QSpinBox()
        self.staging_cleanup_days.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.staging_cleanup_days.setRange(0, 3650)
        self.staging_cleanup_days.setSuffix(" 天")
        stage_form.addRow("保留时间", self.staging_cleanup_days)

        self.staging_cleanup_after_success = QCheckBox("Album 确认成功后删除暂存副本")
        stage_form.addRow("成功清理", self.staging_cleanup_after_success)

        stage_hint = QLabel(
            "启用暂存后，发送前会把源文件复制到本地暂存目录，防止网络盘波动中断；断点仍以原始路径为准。"
        )
        stage_hint.setObjectName("mutedLabel")
        stage_hint.setWordWrap(True)
        stage_layout.addLayout(stage_form)
        stage_layout.addWidget(stage_hint)
        layout.addWidget(stage_card)

        layout.addStretch(1)
        scroll.setWidget(container)
        main_layout.addWidget(scroll)

        # Wire dirty tracking
        self.video_dir.textChanged.connect(self._check_dirty)
        self.image_dir.textChanged.connect(self._check_dirty)
        self.mixed_dir.textChanged.connect(self._check_dirty)
        self.staging_mode.currentIndexChanged.connect(self._check_dirty)
        self.staging_dir.textChanged.connect(self._check_dirty)
        self.staging_cleanup_on_start.toggled.connect(self._check_dirty)
        self.staging_cleanup_days.valueChanged.connect(self._check_dirty)
        self.staging_cleanup_after_success.toggled.connect(self._check_dirty)

        self.load()

    def _browse_dir(self, line_edit: QLineEdit, caption: str):
        chosen = QFileDialog.getExistingDirectory(self, caption, line_edit.text().strip() or "")
        if chosen:
            line_edit.setText(chosen)

    def load(self):
        self.blockSignals(True)
        self.video_dir.setText(str(_cfg("VIDEO_DIR", "")))
        self.image_dir.setText(str(_cfg("IMAGE_DIR", "")))
        self.mixed_dir.setText(str(_cfg("MIXED_DIR", "")))

        configured_staging_mode = str(
            _cfg("STAGING_MODE", "always" if _cfg("STAGING_ENABLED", False) else "off")
        ).strip().lower()
        idx = self.staging_mode.findData(configured_staging_mode)
        self.staging_mode.setCurrentIndex(idx if idx >= 0 else 0)

        self.staging_dir.setText(str(_cfg("STAGING_DIR", STAGING_CACHE_DIR)))
        self.staging_cleanup_on_start.setChecked(bool(_cfg("STAGING_CLEANUP_ON_START", True)))
        self.staging_cleanup_days.setValue(int(_cfg("STAGING_CLEANUP_DAYS", 7)))
        self.staging_cleanup_after_success.setChecked(bool(_cfg("STAGING_CLEANUP_AFTER_SUCCESS", True)))
        self.blockSignals(False)

        self._initial_values = self._current_values_dict()
        self.mark_clean()

    def _current_values_dict(self) -> dict:
        return {
            "video_dir": self.video_dir.text().strip(),
            "image_dir": self.image_dir.text().strip(),
            "mixed_dir": self.mixed_dir.text().strip(),
            "staging_mode": self.staging_mode.currentData() or "off",
            "staging_dir": self.staging_dir.text().strip(),
            "staging_cleanup_on_start": self.staging_cleanup_on_start.isChecked(),
            "staging_cleanup_days": self.staging_cleanup_days.value(),
            "staging_cleanup_after_success": self.staging_cleanup_after_success.isChecked(),
        }

    def _check_dirty(self):
        dirty = self._current_values_dict() != self._initial_values
        self.set_dirty(dirty)

    def collect_values(self) -> dict:
        curr = self._current_values_dict()
        mode = curr["staging_mode"]
        return {
            ("paths", "video_dir"): curr["video_dir"],
            ("paths", "image_dir"): curr["image_dir"],
            ("paths", "mixed_dir"): curr["mixed_dir"],
            ("staging", "enabled"): mode != "off",
            ("staging", "mode"): mode,
            ("staging", "directory"): curr["staging_dir"],
            ("staging", "cleanup_on_start"): curr["staging_cleanup_on_start"],
            ("staging", "cleanup_days"): curr["staging_cleanup_days"],
            ("staging", "cleanup_after_success"): curr["staging_cleanup_after_success"],
        }


__all__ = ["GeneralPanel"]
