# -*- coding: utf-8 -*-
"""Storage paths, log paths, and cache maintenance panel."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...config.paths import (
    DATA_DIR,
    TDLIB_DATABASE_DIR,
    TDLIB_FILES_DIR,
)
from ...core.logging import APP_LOG_PATH, TDLIB_LOG_PATH
from ..icons import get_svg_icon
from .base import SettingsPanel


class StoragePanel(SettingsPanel):
    """Panel managing storage directories, application logs, and cache maintenance."""

    clear_all_requested = Signal()
    clear_thumb_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cache_worker = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # 1. Diagnostic Data & Logs Row (QHBoxLayout layout contract for test_regression_matrix)
        diag_widget = QWidget()
        diag_row = QHBoxLayout(diag_widget)
        diag_row.setContentsMargins(0, 0, 0, 0)
        diag_row.setSpacing(14)

        data_box = QGroupBox("用户数据目录")
        data_layout = QVBoxLayout(data_box)
        data_hint = QLabel(
            f"数据根目录：{DATA_DIR}\n"
            f"TDLib 登录数据库：{TDLIB_DATABASE_DIR}\n"
            f"TDLib 传输缓存：{TDLIB_FILES_DIR}"
        )
        data_hint.setObjectName("mutedLabel")
        data_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        data_hint.setWordWrap(True)
        data_layout.addWidget(data_hint)
        diag_row.addWidget(data_box, 1)

        log_box = QGroupBox("运行日志")
        log_layout = QVBoxLayout(log_box)
        log_hint = QLabel(
            f"应用日志：{APP_LOG_PATH}\n"
            f"TDLib 核心日志：{TDLIB_LOG_PATH}\n"
            "日志会跨应用重启保留；执行“清理所有缓存”时一并清空。"
        )
        log_hint.setObjectName("mutedLabel")
        log_hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        log_hint.setWordWrap(True)
        log_layout.addWidget(log_hint)
        diag_row.addWidget(log_box, 1)

        layout.addWidget(diag_widget)

        # 2. Cache Management Card
        cache_card, cache_layout = self.create_card("存储与缓存维护")

        self.cache_status = QLabel("就绪，点击“重新统计”可统计缓存占用空间")
        self.cache_status.setObjectName("valueLabel")
        self.cache_status.setWordWrap(True)
        cache_layout.addWidget(self.cache_status)

        cache_hint = QLabel(
            "“清理所有”将清空视频/图片/混合断点状态、标题缓存、视频封面、历史记录、未确认记录、暂存副本和日志。\n"
            "不会删除 config.toml 或 Telegram 登录账号会话。"
        )
        cache_hint.setObjectName("mutedLabel")
        cache_hint.setWordWrap(True)
        cache_layout.addWidget(cache_hint)

        cache_buttons = QHBoxLayout()
        calc_cache = QPushButton("重新统计")
        calc_cache.setObjectName("secondaryButton")
        calc_cache.setIcon(get_svg_icon("refresh", 14, 14))
        calc_cache.clicked.connect(self.refresh_cache_stats)

        clear_thumb = QPushButton("仅清理视频封面缓存")
        clear_thumb.setObjectName("secondaryButton")
        clear_thumb.setIcon(get_svg_icon("delete", 14, 14))
        clear_thumb.clicked.connect(lambda: self.clear_thumb_requested.emit())

        clear_all = QPushButton("清理所有缓存与断点")
        clear_all.setObjectName("dangerButton")
        clear_all.setIcon(get_svg_icon("delete", 14, 14, color="#ffffff"))
        clear_all.clicked.connect(lambda: self.clear_all_requested.emit())

        cache_buttons.addWidget(calc_cache)
        cache_buttons.addWidget(clear_thumb)
        cache_buttons.addWidget(clear_all)
        cache_buttons.addStretch(1)
        cache_layout.addLayout(cache_buttons)

        layout.addWidget(cache_card)
        layout.addStretch(1)

        scroll.setWidget(container)
        main_layout.addWidget(scroll)

    def refresh_cache_stats(self):
        """Asynchronously compute cache size on a worker thread to avoid freezing UI."""
        if self._cache_worker is not None and self._cache_worker.isRunning():
            return
        self.cache_status.setText("正在计算缓存占用…")
        from ..workers import CacheStatsWorker  # noqa: PLC0415

        if self._cache_worker is not None:
            self._cache_worker.deleteLater()
        self._cache_worker = CacheStatsWorker(self)
        self._cache_worker.result_ready.connect(self._on_cache_stats_ready)
        self._cache_worker.start()

    def _on_cache_stats_ready(self, text: str):
        self.cache_status.setText(text)

    def load(self):
        self.mark_clean()

    def collect_values(self) -> dict:
        return {}


__all__ = ["StoragePanel"]
