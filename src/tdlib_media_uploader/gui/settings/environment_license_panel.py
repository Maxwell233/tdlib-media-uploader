# -*- coding: utf-8 -*-
"""Environment diagnostics and open-source license panel."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...config.paths import read_version
from ..config_service import get_cfg as _cfg, get_config
from ..icons import get_svg_icon
from .base import SettingsPanel

REPO_URL = "https://github.com/Maxwell233/tdlib-media-uploader"


class EnvironmentLicensePanel(SettingsPanel):
    """Panel managing environment runtime diagnostics and open-source licenses."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.env_labels: dict[str, QLabel] = {}

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        # 1. Section: Runtime Environment & Dependencies
        diag_card, d_layout = self.create_card("运行环境与核心依赖检测")

        env_grid = QGridLayout()
        env_grid.setSpacing(12)

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
            card.setObjectName("envCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 10)
            card_layout.setSpacing(4)

            top = QHBoxLayout()
            name_label = QLabel(name)
            name_label.setObjectName("valueLabel")
            status_label = QLabel("检测中")
            status_label.setObjectName("envStatus")
            self.env_labels[name] = status_label
            top.addWidget(name_label)
            top.addStretch(1)
            top.addWidget(status_label)
            card_layout.addLayout(top)

            desc_label = QLabel(desc)
            desc_label.setObjectName("mutedLabel")
            card_layout.addWidget(desc_label)

            row, col = divmod(idx, 2)
            env_grid.addWidget(card, row, col)

        d_layout.addLayout(env_grid)
        layout.addWidget(diag_card)

        # 2. Section: Application Info & Open-Source Licenses
        about_card, a_layout = self.create_card("应用信息与开源许可")

        version = read_version()
        self.ver_label = QLabel(f"当前版本：{version}")
        self.ver_label.setObjectName("valueLabel")
        a_layout.addWidget(self.ver_label)

        # GitHub Repo Row with hyperlink + button
        repo_row = QHBoxLayout()
        repo_row.setSpacing(12)

        self.repo_link = QLabel(f'项目主页：<a href="{REPO_URL}">{REPO_URL}</a>')
        self.repo_link.setObjectName("valueLabel")
        self.repo_link.setOpenExternalLinks(True)
        self.repo_link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        repo_row.addWidget(self.repo_link, 1)

        self.open_github_btn = QPushButton("在 GitHub 中打开")
        self.open_github_btn.setObjectName("secondaryButton")
        self.open_github_btn.setIcon(get_svg_icon("github", 14, 14))
        self.open_github_btn.clicked.connect(self._open_github)
        repo_row.addWidget(self.open_github_btn)

        a_layout.addLayout(repo_row)

        license_hint = QLabel(
            "TDLib Media Uploader 是一款现代高效的 Telegram 批量媒体上传工具。\n"
            "原创代码采用 GNU General Public License v3.0 only（GPL-3.0-only）授权开源。\n"
            "TDLib、Qt/PySide6、Pillow、FFmpeg、PyInstaller 遵循各自开源许可协议，详见 THIRD_PARTY_LICENSES.md。"
        )
        license_hint.setObjectName("mutedLabel")
        license_hint.setWordWrap(True)
        a_layout.addWidget(license_hint)

        layout.addWidget(about_card)
        layout.addStretch(1)

        scroll.setWidget(container)
        main_layout.addWidget(scroll)

        self.load()

    def _open_github(self):
        QDesktopServices.openUrl(QUrl(REPO_URL))

    def load(self):
        current_cfg = get_config()
        exiftool_path = _cfg("EXIFTOOL_PATH", None)
        checks = {
            "PySide6": True,
            "tdjson": bool(importlib.util.find_spec("tdjson")),
            "Pillow": bool(importlib.util.find_spec("PIL")),
            "imageio-ffmpeg": bool(importlib.util.find_spec("imageio_ffmpeg")),
            "ExifTool": bool(current_cfg is not None and exiftool_path and Path(exiftool_path).exists()),
        }
        for name, available in checks.items():
            label = self.env_labels.get(name)
            if label is not None:
                label.setText("可用" if available else "未找到 / 可选")
                label.setProperty("status", "ok" if available else "warn")
                label.style().unpolish(label)
                label.style().polish(label)

        proxy_label = self.env_labels.get("代理")
        if proxy_label is not None:
            if _cfg("PROXY_ENABLED", False):
                proxy_type = {
                    "socks5": "SOCKS5",
                    "http": "HTTP",
                    "mtproto": "MTProto",
                }.get(str(_cfg("PROXY_TYPE", "socks5")).lower(), "代理")
                proxy_label.setText(
                    f"已启用 · {proxy_type} {_cfg('PROXY_SERVER', '')}:{_cfg('PROXY_PORT', '')}"
                )
                proxy_label.setProperty("status", "ok")
            else:
                proxy_label.setText("未启用 · 直连")
                proxy_label.setProperty("status", "muted")
            proxy_label.style().unpolish(proxy_label)
            proxy_label.style().polish(proxy_label)

        version = read_version()
        self.ver_label.setText(f"当前版本：{version}")
        self.mark_clean()

    def collect_values(self) -> dict:
        return {}


__all__ = ["EnvironmentLicensePanel"]
