# -*- coding: utf-8 -*-
"""Independent scan tools, external process and concurrency dialog for Beta 4."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...config.paths import read_version
from .. import config_service, tools
from ..config_service import get_cfg as _cfg


class ScanToolsDialog(QDialog):
    """Edit scan stability, media-tool timeouts, and external-process parameters."""

    def __init__(self, parent=None):
        super().__init__(parent)
        version = read_version()
        self.setWindowTitle(f"扫描与外部工具 · V{version}")
        self.setMinimumWidth(640)
        self.resize(720, 680)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(14)
        self.fields = {}

        def field(key: str, value):
            widget = QLineEdit(str(value if value is not None else ""))
            self.fields[key] = widget
            return widget

        # External Tool
        tool_box = QGroupBox("外部工具")
        tool_form = QFormLayout(tool_box)
        tool_form.setSpacing(10)
        default_exiftool = "tools/exiftool.exe" if os.name == "nt" else "tools/exiftool"
        tool_form.addRow(
            "ExifTool 路径",
            field("exiftool_path", _cfg("EXIFTOOL_PATH", default_exiftool)),
        )
        tool_hint = QLabel(
            "ExifTool 用于高精度提取照片/视频拍摄日期；FFmpeg 用于视频封面提取和超限压缩。\n"
            "路径留空时将自动使用随发布包提供的默认工具。"
        )
        tool_hint.setObjectName("mutedLabel")
        tool_hint.setWordWrap(True)
        tool_form.addRow("说明", tool_hint)
        content_layout.addWidget(tool_box)

        # Scan stability & Concurrency
        scan_box = QGroupBox("扫描稳定性与并发")
        scan_form = QFormLayout(scan_box)
        scan_form.setSpacing(10)

        def integer_option(value, minimum, maximum, suffix=""):
            widget = QSpinBox()
            widget.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            widget.setRange(minimum, maximum)
            widget.setValue(int(value))
            if suffix:
                widget.setSuffix(suffix)
            return widget

        def decimal_option(value, minimum, maximum, decimals=2, suffix=""):
            widget = QDoubleSpinBox()
            widget.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
            widget.setRange(minimum, maximum)
            widget.setDecimals(decimals)
            widget.setValue(float(value))
            if suffix:
                widget.setSuffix(suffix)
            return widget

        self.scan_stability_checks = integer_option(
            _cfg("SCAN_STABILITY_CHECKS", 2), 1, 8, " 次"
        )
        self.scan_stability_interval = decimal_option(
            _cfg("SCAN_STABILITY_INTERVAL_SECONDS", 0.05), 0.0, 5.0, 2, " 秒"
        )
        self.scan_stability_checks_local = integer_option(
            _cfg("SCAN_STABILITY_CHECKS_LOCAL", 2), 1, 8, " 次"
        )
        self.scan_stability_interval_local = decimal_option(
            _cfg("SCAN_STABILITY_INTERVAL_LOCAL_SECONDS", 0.05), 0.0, 30.0, 2, " 秒"
        )
        self.scan_stability_checks_network = integer_option(
            _cfg("SCAN_STABILITY_CHECKS_NETWORK", 3), 1, 8, " 次"
        )
        self.scan_stability_interval_network = decimal_option(
            _cfg("SCAN_STABILITY_INTERVAL_NETWORK_SECONDS", 0.5), 0.0, 60.0, 2, " 秒"
        )
        self.scan_discovery_attempts = integer_option(
            _cfg("SCAN_DISCOVERY_ATTEMPTS", 3), 1, 8, " 次"
        )
        self.scan_discovery_initial_delay = decimal_option(
            _cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15), 0.0, 10.0, 2, " 秒"
        )
        self.scan_discovery_max_delay = decimal_option(
            _cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0), 0.0, 60.0, 2, " 秒"
        )
        self.scan_readiness_attempts = integer_option(
            _cfg("SCAN_READINESS_ATTEMPTS", 3), 1, 8, " 次"
        )
        self.scan_probe_bytes = integer_option(
            _cfg("SCAN_READ_PROBE_BYTES", 65536), 1, 4 * 1024 * 1024, " 字节"
        )
        self.scan_workers_local = integer_option(
            _cfg("IO_WORKERS_LOCAL", 4), 1, 32, " 个"
        )
        self.scan_workers_network = integer_option(
            _cfg("IO_WORKERS_NETWORK", 2), 1, 16, " 个"
        )
        scan_form.addRow("稳定性检查次数", self.scan_stability_checks)
        scan_form.addRow("稳定性检查间隔", self.scan_stability_interval)
        scan_form.addRow("本地稳定检查次数", self.scan_stability_checks_local)
        scan_form.addRow("本地稳定检查间隔", self.scan_stability_interval_local)
        scan_form.addRow("网络稳定检查次数", self.scan_stability_checks_network)
        scan_form.addRow("网络稳定检查间隔", self.scan_stability_interval_network)
        scan_form.addRow("目录发现重试次数", self.scan_discovery_attempts)
        scan_form.addRow("发现首次等待", self.scan_discovery_initial_delay)
        scan_form.addRow("发现最大等待", self.scan_discovery_max_delay)
        scan_form.addRow("不可读重试次数", self.scan_readiness_attempts)
        scan_form.addRow("读探针大小", self.scan_probe_bytes)
        scan_form.addRow("本地 I/O 并发", self.scan_workers_local)
        scan_form.addRow("网络 I/O 并发", self.scan_workers_network)
        content_layout.addWidget(scan_box)

        # Process Timeouts & Batching
        process_box = QGroupBox("3 · 外部进程超时与批处理")
        process_form = QFormLayout(process_box)
        process_form.setSpacing(10)
        self.process_timeouts = {}
        for key, label, config_key, default in (
            ("exiftool_timeout_seconds", "ExifTool 超时", "EXIFTOOL_TIMEOUT_SECONDS", 120),
            ("ffmpeg_metadata_timeout_seconds", "FFmpeg 日期超时", "FFMPEG_METADATA_TIMEOUT_SECONDS", 30),
            ("ffmpeg_thumbnail_timeout_seconds", "FFmpeg 封面超时", "FFMPEG_THUMBNAIL_TIMEOUT_SECONDS", 30),
            ("ffmpeg_compress_timeout_seconds", "FFmpeg 压缩超时", "FFMPEG_COMPRESS_TIMEOUT_SECONDS", 60),
        ):
            widget = integer_option(_cfg(config_key, default), 1, 3600, " 秒")
            self.process_timeouts[key] = widget
            process_form.addRow(label, widget)

        self.exiftool_batch_size = integer_option(
            _cfg("EXIFTOOL_BATCH_SIZE", 64), 1, 512, " 个"
        )
        process_form.addRow("ExifTool 批次大小", self.exiftool_batch_size)
        self.exiftool_retries = integer_option(_cfg("EXIFTOOL_RETRIES", 2), 0, 5, " 次")
        process_form.addRow("ExifTool 重试次数", self.exiftool_retries)
        content_layout.addWidget(process_box)

        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        exiftool_path = self.fields["exiftool_path"].text().strip()
        validation_error = tools.validate_exiftool_path(exiftool_path)
        if validation_error:
            QMessageBox.warning(self, "ExifTool 路径不可用", validation_error)
            return
        values = {
            ("paths", "exiftool_path"): exiftool_path,
            ("scan", "stability_checks"): self.scan_stability_checks.value(),
            ("scan", "stability_interval_seconds"): self.scan_stability_interval.value(),
            ("scan", "stability_checks_local"): self.scan_stability_checks_local.value(),
            ("scan", "stability_interval_local_seconds"): self.scan_stability_interval_local.value(),
            ("scan", "stability_checks_network"): self.scan_stability_checks_network.value(),
            ("scan", "stability_interval_network_seconds"): self.scan_stability_interval_network.value(),
            ("scan", "discovery_attempts"): self.scan_discovery_attempts.value(),
            ("scan", "discovery_initial_delay_seconds"): self.scan_discovery_initial_delay.value(),
            ("scan", "discovery_max_delay_seconds"): self.scan_discovery_max_delay.value(),
            ("scan", "readiness_attempts"): self.scan_readiness_attempts.value(),
            ("scan", "read_probe_bytes"): self.scan_probe_bytes.value(),
            ("scan", "io_workers_local"): self.scan_workers_local.value(),
            ("scan", "io_workers_network"): self.scan_workers_network.value(),
        }
        values.update({("process", key): widget.value() for key, widget in self.process_timeouts.items()})
        values[("process", "exiftool_batch_size")] = self.exiftool_batch_size.value()
        values[("process", "exiftool_retries")] = self.exiftool_retries.value()
        error = config_service.write_config_values(values)
        if error:
            QMessageBox.critical(self, "保存失败", error)
            return
        self.accept()


__all__ = ["ScanToolsDialog"]
