# -*- coding: utf-8 -*-
"""Modern settings and diagnostics page for Beta 3."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...config.paths import (
    CONFIG_PATH,
    DATA_DIR,
    HISTORY_PATH,
    LOG_DIR,
    TDLIB_DATABASE_DIR,
    TDLIB_FILES_DIR,
    read_version,
)
from ...core.logging import APP_LOG_PATH, TDLIB_LOG_PATH
from ..components.status_pill import StatusPill
from ..theme import THEME


def _cfg(name: str, default=None):
    from ..main_window import _cfg as get_cfg

    return get_cfg(name, default)


def _cache_status_text() -> str:
    from ..main_window import _cache_status_text as get_cache_status

    return get_cache_status()


class SettingsPage(QWidget):
    """Modern hub for application configuration, runtime health, and cache maintenance."""

    open_editor = Signal()
    open_scan_tools = Signal()
    clear_all_requested = Signal()
    clear_thumb_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        # Title
        header = QHBoxLayout()
        title = QLabel("设置与系统诊断")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)
        layout.addLayout(header)

        # Config Status Banner
        self.config_banner = QFrame()
        self.config_banner.setObjectName("bannerFrame")
        banner_layout = QHBoxLayout(self.config_banner)
        banner_layout.setContentsMargins(16, 12, 16, 12)
        self.config_status = QLabel()
        self.config_status.setWordWrap(True)
        self.config_status.setObjectName("valueLabel")
        banner_layout.addWidget(self.config_status, 1)
        layout.addWidget(self.config_banner)

        # Runtime Environment Card
        env_box = QGroupBox("1 · 运行环境与依赖检测")
        env_layout = QGridLayout(env_box)
        env_layout.setSpacing(12)
        self.env_labels: dict[str, QLabel] = {}

        deps = [
            ("PySide6", "Qt 桌面图形界面库"),
            ("tdjson", "TDLib 原生 JSON 客户端核心"),
            ("Pillow", "图像处理与尺寸分析库"),
            ("imageio-ffmpeg", "视频元数据与缩略图提取核心"),
            ("ExifTool", "高精度 EXIF 媒体拍摄日期工具"),
            ("代理", "Telegram 专线网络连接"),
        ]
        for idx, (name, desc) in enumerate(deps):
            card = QFrame()
            card.setStyleSheet(
                f"background-color: {THEME.bg_card}; border: 1px solid {THEME.border_subtle}; "
                f"border-radius: 8px; padding: 10px 12px;"
            )
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(0, 0, 0, 0)
            card_layout.setSpacing(4)

            top = QHBoxLayout()
            name_label = QLabel(name)
            name_label.setObjectName("valueLabel")
            status_label = QLabel("检测中")
            self.env_labels[name] = status_label
            top.addWidget(name_label)
            top.addStretch(1)
            top.addWidget(status_label)
            card_layout.addLayout(top)

            desc_label = QLabel(desc)
            desc_label.setObjectName("mutedLabel")
            card_layout.addWidget(desc_label)

            row, col = divmod(idx, 2)
            env_layout.addWidget(card, row, col)

        layout.addWidget(env_box)

        # Configuration Entrances
        config_box = QGroupBox("2 · 配置入口")
        config_layout = QVBoxLayout(config_box)
        config_layout.setSpacing(10)

        buttons_row = QHBoxLayout()
        edit = QPushButton("编辑基础配置（账号、目录、暂存、代理）")
        edit.setObjectName("primaryButton")
        edit.clicked.connect(self.open_editor)

        scan_tools = QPushButton("扫描与外部工具设置（ExifTool、超时、并发）")
        scan_tools.setObjectName("secondaryButton")
        scan_tools.clicked.connect(self.open_scan_tools)

        buttons_row.addWidget(edit)
        buttons_row.addWidget(scan_tools)
        buttons_row.addStretch(1)
        config_layout.addLayout(buttons_row)

        config_hint = QLabel(
            "提示：修改配置时会保留 config.toml 原有注释与排版。\n"
            "视频、图片和混合上传的目标 Chat ID、Topic ID 与分组选项，请在对应上传页面的“编辑目标”中修改。"
        )
        config_hint.setObjectName("mutedLabel")
        config_hint.setWordWrap(True)
        config_layout.addWidget(config_hint)
        layout.addWidget(config_box)

        # Diagnostic Data & Logs Row
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

        # Cache Management
        cache_box = QGroupBox("5 · 存储与缓存维护")
        cache_layout = QVBoxLayout(cache_box)
        cache_layout.setSpacing(10)

        self.cache_status = QLabel()
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
        clear_thumb = QPushButton("仅清理视频封面缓存")
        clear_thumb.setObjectName("secondaryButton")
        clear_thumb.clicked.connect(lambda: self.clear_thumb_requested.emit())

        clear_all = QPushButton("清理所有缓存与断点")
        clear_all.setObjectName("dangerButton")
        clear_all.clicked.connect(lambda: self.clear_all_requested.emit())

        cache_buttons.addWidget(clear_thumb)
        cache_buttons.addWidget(clear_all)
        cache_buttons.addStretch(1)
        cache_layout.addLayout(cache_buttons)
        layout.addWidget(cache_box)

        # License
        license_box = QGroupBox("6 · 开源许可与版权")
        license_layout = QVBoxLayout(license_box)
        version = read_version()
        license_hint = QLabel(
            f"TDLib Media Uploader · 版本 {version}\n"
            "原创代码采用 GNU General Public License v3.0 only（GPL-3.0-only）。\n"
            "TDLib、Qt/PySide6、Pillow、FFmpeg、PyInstaller 遵循各自开源许可协议，详见 THIRD_PARTY_LICENSES.md。"
        )
        license_hint.setObjectName("mutedLabel")
        license_hint.setWordWrap(True)
        license_layout.addWidget(license_hint)
        layout.addWidget(license_box)

        layout.addStretch(1)
        self.refresh()

    def refresh(self):
        from ..main_window import _CONFIG_ERROR, cfg

        if _CONFIG_ERROR:
            self.config_status.setText(f"配置不可用：{_CONFIG_ERROR}")
            self.config_status.setStyleSheet(f"color: {THEME.danger};")
        elif _cfg("API_ID", 12345678) == 12345678 or _cfg("API_HASH", "YOUR_API_HASH") == "YOUR_API_HASH":
            self.config_status.setText("配置文件已找到，但 Telegram API 仍为示例值，请先编辑配置。")
            self.config_status.setStyleSheet(f"color: {THEME.warning};")
        else:
            self.config_status.setText(f"配置文件已加载：{CONFIG_PATH}")
            self.config_status.setStyleSheet(f"color: {THEME.success};")

        exiftool_path = _cfg("EXIFTOOL_PATH", None)
        checks = {
            "PySide6": True,
            "tdjson": bool(importlib.util.find_spec("tdjson")),
            "Pillow": bool(importlib.util.find_spec("PIL")),
            "imageio-ffmpeg": bool(importlib.util.find_spec("imageio_ffmpeg")),
            "ExifTool": bool(cfg is not None and exiftool_path and Path(exiftool_path).exists()),
        }
        for name, available in checks.items():
            label = self.env_labels[name]
            label.setText("可用" if available else "未找到 / 可选")
            label.setStyleSheet(f"color: {THEME.success if available else THEME.warning}; font-weight: 600;")

        proxy_label = self.env_labels["代理"]
        if _cfg("PROXY_ENABLED", False):
            proxy_type = {
                "socks5": "SOCKS5",
                "http": "HTTP",
                "mtproto": "MTProto",
            }.get(str(_cfg("PROXY_TYPE", "socks5")).lower(), "代理")
            proxy_label.setText(
                f"已启用 · {proxy_type} {_cfg('PROXY_SERVER', '')}:{_cfg('PROXY_PORT', '')}"
            )
            proxy_label.setStyleSheet(f"color: {THEME.success}; font-weight: 600;")
        else:
            proxy_label.setText("未启用 · 直连")
            proxy_label.setStyleSheet(f"color: {THEME.text_muted};")

        self.cache_status.setText(_cache_status_text())


__all__ = ["SettingsPage"]
