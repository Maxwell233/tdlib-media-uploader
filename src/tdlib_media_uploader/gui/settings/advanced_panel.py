# -*- coding: utf-8 -*-
"""Advanced scan, stability, concurrency and process options panel."""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import tools
from ..config_service import get_cfg as _cfg
from ..icons import get_svg_icon
from .base import SettingsPanel


class AdvancedPanel(SettingsPanel):
    """Panel managing ExifTool path, I/O concurrency, timeouts and scan stability."""

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
        layout.setSpacing(14)

        # Top Notice Banner
        banner = QFrame()
        banner.setObjectName("bannerFrame")
        b_layout = QVBoxLayout(banner)
        b_layout.setContentsMargins(14, 12, 14, 12)
        b_layout.setSpacing(4)
        b_title = QLabel("高级选项")
        b_title.setObjectName("sectionTitle")
        b_text = QLabel(
            "这些设置一般无需修改。仅在扫描异常、外部工具未被正确检测，或需要针对特殊存储设备（如慢速 NAS/SMB）进行性能调优时调整。"
        )
        b_text.setObjectName("mutedLabel")
        b_text.setWordWrap(True)
        b_layout.addWidget(b_title)
        b_layout.addWidget(b_text)
        layout.addWidget(banner)

        # 1. External Tools Card (Always Expanded)
        tool_card, tool_layout = self.create_card("外部工具路径")
        tool_form = QFormLayout()
        tool_form.setSpacing(10)

        self.exiftool_path = QLineEdit()
        tool_row = QHBoxLayout()
        tool_row.setSpacing(8)
        tool_row.addWidget(self.exiftool_path, 1)
        browse_tool_btn = QPushButton("选择")
        browse_tool_btn.setObjectName("secondaryButton")
        browse_tool_btn.setIcon(get_svg_icon("folder", 14, 14))
        browse_tool_btn.clicked.connect(self._browse_exiftool)
        tool_row.addWidget(browse_tool_btn)
        tool_form.addRow("ExifTool", tool_row)

        ffmpeg_status_str = "自动检测：可用" if tools.detect_ffmpeg() else "自动检测：未找到（可选）"
        self.ffmpeg_label = QLabel(ffmpeg_status_str)
        self.ffmpeg_label.setObjectName("valueLabel")
        tool_form.addRow("FFmpeg", self.ffmpeg_label)

        tool_hint = QLabel("ExifTool 用于提取照片与视频拍摄日期；FFmpeg 用于封面提取与超限压缩。")
        tool_hint.setObjectName("mutedLabel")
        tool_hint.setWordWrap(True)
        tool_layout.addLayout(tool_form)
        tool_layout.addWidget(tool_hint)
        layout.addWidget(tool_card)

        # 2. Concurrency Card (Always Expanded)
        io_card, io_layout = self.create_card("扫描 I/O 并发线程")
        io_form = QFormLayout()
        io_form.setSpacing(10)

        self.scan_workers_local = self._int_spin(1, 32, " 个")
        self.scan_workers_network = self._int_spin(1, 16, " 个")
        io_form.addRow("本地 I/O 并发", self.scan_workers_local)
        io_form.addRow("网络 I/O 并发", self.scan_workers_network)

        io_layout.addLayout(io_form)
        layout.addWidget(io_card)

        # Helper for collapsible section
        def build_collapsible(title: str):
            c_card = QFrame()
            c_card.setObjectName("surfaceCard")
            c_card_layout = QVBoxLayout(c_card)
            c_card_layout.setContentsMargins(16, 12, 16, 12)
            c_card_layout.setSpacing(10)

            header_row = QHBoxLayout()
            header_btn = QPushButton(f"▶ {title}")
            header_btn.setObjectName("ghostButton")
            header_btn.setCheckable(True)
            header_btn.setChecked(False)
            header_row.addWidget(header_btn, 0)
            header_row.addStretch(1)
            c_card_layout.addLayout(header_row)

            inner = QWidget()
            inner_layout = QFormLayout(inner)
            inner_layout.setContentsMargins(0, 4, 0, 4)
            inner_layout.setSpacing(10)
            inner.setVisible(False)
            c_card_layout.addWidget(inner)

            def on_toggled(checked):
                inner.setVisible(checked)
                header_btn.setText(f"{'▼' if checked else '▶'} {title}")

            header_btn.toggled.connect(on_toggled)
            return c_card, inner_layout

        # 3. Collapsible: File Stability
        stab_card, stab_form = build_collapsible("文件稳定性参数（防止写入中被误扫）")
        self.scan_stability_checks = self._int_spin(1, 8, " 次")
        self.scan_stability_interval = self._dec_spin(0.0, 5.0, 2, " 秒")
        self.scan_stability_checks_local = self._int_spin(1, 8, " 次")
        self.scan_stability_interval_local = self._dec_spin(0.0, 30.0, 2, " 秒")
        self.scan_stability_checks_network = self._int_spin(1, 8, " 次")
        self.scan_stability_interval_network = self._dec_spin(0.0, 60.0, 2, " 秒")

        stab_form.addRow("稳定检查次数", self.scan_stability_checks)
        stab_form.addRow("稳定检查间隔", self.scan_stability_interval)
        stab_form.addRow("本地稳定检查次数", self.scan_stability_checks_local)
        stab_form.addRow("本地稳定检查间隔", self.scan_stability_interval_local)
        stab_form.addRow("网络稳定检查次数", self.scan_stability_checks_network)
        stab_form.addRow("网络稳定检查间隔", self.scan_stability_interval_network)
        layout.addWidget(stab_card)

        # 4. Collapsible: Process Timeouts
        proc_card, proc_form = build_collapsible("外部进程超时控制")
        self.exiftool_timeout = self._int_spin(1, 3600, " 秒")
        self.ffmpeg_metadata_timeout = self._int_spin(1, 3600, " 秒")
        self.ffmpeg_thumbnail_timeout = self._int_spin(1, 3600, " 秒")
        self.ffmpeg_compress_timeout = self._int_spin(1, 3600, " 秒")

        proc_form.addRow("ExifTool 超时", self.exiftool_timeout)
        proc_form.addRow("FFmpeg 日期超时", self.ffmpeg_metadata_timeout)
        proc_form.addRow("FFmpeg 封面超时", self.ffmpeg_thumbnail_timeout)
        proc_form.addRow("FFmpeg 压缩超时", self.ffmpeg_compress_timeout)
        layout.addWidget(proc_card)

        # 5. Collapsible: Retries & Batching
        retry_card, retry_form = build_collapsible("重试与批处理大小")
        self.scan_discovery_attempts = self._int_spin(1, 8, " 次")
        self.scan_discovery_initial_delay = self._dec_spin(0.0, 10.0, 2, " 秒")
        self.scan_discovery_max_delay = self._dec_spin(0.0, 60.0, 2, " 秒")
        self.scan_readiness_attempts = self._int_spin(1, 8, " 次")
        self.scan_probe_bytes = self._int_spin(1, 4 * 1024 * 1024, " 字节")
        self.exiftool_batch_size = self._int_spin(1, 512, " 个")
        self.exiftool_retries = self._int_spin(0, 5, " 次")

        retry_form.addRow("目录发现重试", self.scan_discovery_attempts)
        retry_form.addRow("发现首次等待", self.scan_discovery_initial_delay)
        retry_form.addRow("发现最大等待", self.scan_discovery_max_delay)
        retry_form.addRow("不可读重试次数", self.scan_readiness_attempts)
        retry_form.addRow("读探针字节", self.scan_probe_bytes)
        retry_form.addRow("ExifTool 批次大小", self.exiftool_batch_size)
        retry_form.addRow("ExifTool 重试次数", self.exiftool_retries)
        layout.addWidget(retry_card)

        layout.addStretch(1)
        scroll.setWidget(container)
        main_layout.addWidget(scroll)

        # Wire dirty tracking
        self.exiftool_path.textChanged.connect(self._check_dirty)
        for spin in (
            self.scan_workers_local, self.scan_workers_network,
            self.scan_stability_checks, self.scan_stability_checks_local, self.scan_stability_checks_network,
            self.exiftool_timeout, self.ffmpeg_metadata_timeout, self.ffmpeg_thumbnail_timeout, self.ffmpeg_compress_timeout,
            self.scan_discovery_attempts, self.scan_readiness_attempts, self.scan_probe_bytes,
            self.exiftool_batch_size, self.exiftool_retries,
        ):
            spin.valueChanged.connect(self._check_dirty)

        for dspin in (
            self.scan_stability_interval, self.scan_stability_interval_local, self.scan_stability_interval_network,
            self.scan_discovery_initial_delay, self.scan_discovery_max_delay,
        ):
            dspin.valueChanged.connect(self._check_dirty)

        self.load()

    def _int_spin(self, minimum: int, maximum: int, suffix: str = "") -> QSpinBox:
        w = QSpinBox()
        w.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        w.setRange(minimum, maximum)
        if suffix:
            w.setSuffix(suffix)
        return w

    def _dec_spin(self, minimum: float, maximum: float, decimals: int = 2, suffix: str = "") -> QDoubleSpinBox:
        w = QDoubleSpinBox()
        w.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        w.setRange(minimum, maximum)
        w.setDecimals(decimals)
        if suffix:
            w.setSuffix(suffix)
        return w

    def _browse_exiftool(self):
        chosen, _ = QFileDialog.getOpenFileName(
            self, "选择 ExifTool 执行程序", self.exiftool_path.text().strip() or ""
        )
        if chosen:
            self.exiftool_path.setText(chosen)

    def load(self):
        self.blockSignals(True)
        default_exif = "tools/exiftool.exe" if os.name == "nt" else "tools/exiftool"
        self.exiftool_path.setText(str(_cfg("EXIFTOOL_PATH", default_exif)))

        self.scan_workers_local.setValue(int(_cfg("IO_WORKERS_LOCAL", 4)))
        self.scan_workers_network.setValue(int(_cfg("IO_WORKERS_NETWORK", 2)))

        self.scan_stability_checks.setValue(int(_cfg("SCAN_STABILITY_CHECKS", 2)))
        self.scan_stability_interval.setValue(float(_cfg("SCAN_STABILITY_INTERVAL_SECONDS", 0.05)))
        self.scan_stability_checks_local.setValue(int(_cfg("SCAN_STABILITY_CHECKS_LOCAL", 2)))
        self.scan_stability_interval_local.setValue(float(_cfg("SCAN_STABILITY_INTERVAL_LOCAL_SECONDS", 0.05)))
        self.scan_stability_checks_network.setValue(int(_cfg("SCAN_STABILITY_CHECKS_NETWORK", 3)))
        self.scan_stability_interval_network.setValue(float(_cfg("SCAN_STABILITY_INTERVAL_NETWORK_SECONDS", 0.5)))

        self.exiftool_timeout.setValue(int(_cfg("EXIFTOOL_TIMEOUT_SECONDS", 120)))
        self.ffmpeg_metadata_timeout.setValue(int(_cfg("FFMPEG_METADATA_TIMEOUT_SECONDS", 30)))
        self.ffmpeg_thumbnail_timeout.setValue(int(_cfg("FFMPEG_THUMBNAIL_TIMEOUT_SECONDS", 30)))
        self.ffmpeg_compress_timeout.setValue(int(_cfg("FFMPEG_COMPRESS_TIMEOUT_SECONDS", 60)))

        self.scan_discovery_attempts.setValue(int(_cfg("SCAN_DISCOVERY_ATTEMPTS", 3)))
        self.scan_discovery_initial_delay.setValue(float(_cfg("SCAN_DISCOVERY_INITIAL_DELAY_SECONDS", 0.15)))
        self.scan_discovery_max_delay.setValue(float(_cfg("SCAN_DISCOVERY_MAX_DELAY_SECONDS", 1.0)))
        self.scan_readiness_attempts.setValue(int(_cfg("SCAN_READINESS_ATTEMPTS", 3)))
        self.scan_probe_bytes.setValue(int(_cfg("SCAN_READ_PROBE_BYTES", 65536)))
        self.exiftool_batch_size.setValue(int(_cfg("EXIFTOOL_BATCH_SIZE", 64)))
        self.exiftool_retries.setValue(int(_cfg("EXIFTOOL_RETRIES", 2)))

        self.blockSignals(False)
        self._initial_values = self._current_values_dict()
        self.mark_clean()

    def _current_values_dict(self) -> dict:
        return {
            "exiftool_path": self.exiftool_path.text().strip(),
            "scan_workers_local": self.scan_workers_local.value(),
            "scan_workers_network": self.scan_workers_network.value(),
            "scan_stability_checks": self.scan_stability_checks.value(),
            "scan_stability_interval": self.scan_stability_interval.value(),
            "scan_stability_checks_local": self.scan_stability_checks_local.value(),
            "scan_stability_interval_local": self.scan_stability_interval_local.value(),
            "scan_stability_checks_network": self.scan_stability_checks_network.value(),
            "scan_stability_interval_network": self.scan_stability_interval_network.value(),
            "exiftool_timeout": self.exiftool_timeout.value(),
            "ffmpeg_metadata_timeout": self.ffmpeg_metadata_timeout.value(),
            "ffmpeg_thumbnail_timeout": self.ffmpeg_thumbnail_timeout.value(),
            "ffmpeg_compress_timeout": self.ffmpeg_compress_timeout.value(),
            "scan_discovery_attempts": self.scan_discovery_attempts.value(),
            "scan_discovery_initial_delay": self.scan_discovery_initial_delay.value(),
            "scan_discovery_max_delay": self.scan_discovery_max_delay.value(),
            "scan_readiness_attempts": self.scan_readiness_attempts.value(),
            "scan_probe_bytes": self.scan_probe_bytes.value(),
            "exiftool_batch_size": self.exiftool_batch_size.value(),
            "exiftool_retries": self.exiftool_retries.value(),
        }

    def _check_dirty(self):
        self.set_dirty(self._current_values_dict() != self._initial_values)

    def validate(self) -> tuple[bool, str]:
        path = self.exiftool_path.text().strip()
        err = tools.validate_exiftool_path(path)
        if err:
            return False, f"ExifTool 路径无效：{err}"
        return True, ""

    def collect_values(self) -> dict:
        c = self._current_values_dict()
        return {
            ("paths", "exiftool_path"): c["exiftool_path"],
            ("scan", "io_workers_local"): c["scan_workers_local"],
            ("scan", "io_workers_network"): c["scan_workers_network"],
            ("scan", "stability_checks"): c["scan_stability_checks"],
            ("scan", "stability_interval_seconds"): c["scan_stability_interval"],
            ("scan", "stability_checks_local"): c["scan_stability_checks_local"],
            ("scan", "stability_interval_local_seconds"): c["scan_stability_interval_local"],
            ("scan", "stability_checks_network"): c["scan_stability_checks_network"],
            ("scan", "stability_interval_network_seconds"): c["scan_stability_interval_network"],
            ("process", "exiftool_timeout_seconds"): c["exiftool_timeout"],
            ("process", "ffmpeg_metadata_timeout_seconds"): c["ffmpeg_metadata_timeout"],
            ("process", "ffmpeg_thumbnail_timeout_seconds"): c["ffmpeg_thumbnail_timeout"],
            ("process", "ffmpeg_compress_timeout_seconds"): c["ffmpeg_compress_timeout"],
            ("scan", "discovery_attempts"): c["scan_discovery_attempts"],
            ("scan", "discovery_initial_delay_seconds"): c["scan_discovery_initial_delay"],
            ("scan", "discovery_max_delay_seconds"): c["scan_discovery_max_delay"],
            ("scan", "readiness_attempts"): c["scan_readiness_attempts"],
            ("scan", "read_probe_bytes"): c["scan_probe_bytes"],
            ("process", "exiftool_batch_size"): c["exiftool_batch_size"],
            ("process", "exiftool_retries"): c["exiftool_retries"],
        }


__all__ = ["AdvancedPanel"]
