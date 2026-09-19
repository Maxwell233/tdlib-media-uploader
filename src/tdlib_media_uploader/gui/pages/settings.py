# -*- coding: utf-8 -*-
"""Modern two-level settings and diagnostics page for Beta 4."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
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
from ..cache_service import cache_status_text
from ..config_service import _CONFIG_ERROR, get_cfg, get_config
from ..icons import get_svg_icon
from ..theme import THEME


_cfg = get_cfg
_cache_status_text = cache_status_text


class SettingsPage(QWidget):
    """Modern two-level hub for application configuration, runtime health, and cache maintenance."""

    open_editor = Signal()
    open_scan_tools = Signal()
    clear_all_requested = Signal()
    clear_thumb_requested = Signal()

    SETTINGS_CATEGORIES = (
        ("常规", "settings"),
        ("Telegram", "telegram"),
        ("上传参数", "upload"),
        ("扫描与工具", "search"),
        ("存储与缓存", "database"),
        ("环境诊断", "task"),
        ("关于与许可", "info"),
    )

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

        # Right content stack
        self.stack = QStackedWidget()
        body_layout.addWidget(self.stack, 1)
        layout.addLayout(body_layout, 1)

        # Connect sub-navigation
        self.nav_list.currentRowChanged.connect(self.stack.setCurrentIndex)

        # Track secondary buttons with theme-adaptive icons
        self.theme_icon_buttons: list[tuple[QPushButton, str]] = []

        # Build category pages
        self._build_general_tab()
        self._build_telegram_tab()
        self._build_upload_tab()
        self._build_scan_tools_tab()
        self._build_storage_tab()
        self._build_diagnostics_tab()
        self._build_about_tab()

        self.nav_list.setCurrentRow(0)
        self.refresh()

    def _build_general_tab(self):
        page = QWidget()
        p_layout = QVBoxLayout(page)
        p_layout.setContentsMargins(0, 0, 0, 0)
        p_layout.setSpacing(16)

        # Banner Card
        self.config_banner = QFrame()
        self.config_banner.setObjectName("surfaceCard")
        b_layout = QVBoxLayout(self.config_banner)
        b_layout.setContentsMargins(18, 16, 18, 16)
        b_layout.setSpacing(10)

        top_row = QHBoxLayout()
        banner_title = QLabel("配置文件状态")
        banner_title.setObjectName("cardTitle")
        top_row.addWidget(banner_title)
        top_row.addStretch(1)
        b_layout.addLayout(top_row)

        self.config_status = QLabel()
        self.config_status.setWordWrap(True)
        self.config_status.setObjectName("configStatus")
        b_layout.addWidget(self.config_status)

        p_layout.addWidget(self.config_banner)

        # Actions Card
        actions_card = QFrame()
        actions_card.setObjectName("surfaceCard")
        a_layout = QVBoxLayout(actions_card)
        a_layout.setContentsMargins(18, 16, 18, 16)
        a_layout.setSpacing(12)

        a_title = QLabel("配置操作入口")
        a_title.setObjectName("cardTitle")
        a_layout.addWidget(a_title)

        btn_row = QHBoxLayout()
        edit_btn = QPushButton("编辑基础配置（账号、目录、暂存、代理）")
        edit_btn.setObjectName("primaryButton")
        edit_btn.setIcon(get_svg_icon("edit", 14, 14, color="#ffffff"))
        edit_btn.clicked.connect(self.open_editor)

        scan_btn = QPushButton("扫描与外部工具设置（ExifTool、超时、并发）")
        scan_btn.setObjectName("secondaryButton")
        scan_btn.setIcon(get_svg_icon("search", 14, 14))
        scan_btn.clicked.connect(self.open_scan_tools)
        self.theme_icon_buttons.append((scan_btn, "search"))

        btn_row.addWidget(edit_btn)
        btn_row.addWidget(scan_btn)
        btn_row.addStretch(1)
        a_layout.addLayout(btn_row)

        hint = QLabel(
            "提示：修改配置时会保留 config.toml 原有注释与排版。\n"
            "视频、图片和混合上传的目标 Chat ID、Topic ID 与分组选项，请在对应上传工作台的“编辑目标”中调整。"
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        a_layout.addWidget(hint)

        p_layout.addWidget(actions_card)
        p_layout.addStretch(1)
        self.stack.addWidget(page)

    def _build_telegram_tab(self):
        page = QWidget()
        p_layout = QVBoxLayout(page)
        p_layout.setContentsMargins(0, 0, 0, 0)
        p_layout.setSpacing(16)

        card = QFrame()
        card.setObjectName("surfaceCard")
        c_layout = QVBoxLayout(card)
        c_layout.setContentsMargins(18, 16, 18, 16)
        c_layout.setSpacing(12)

        t_title = QLabel("Telegram API 与网络专线")
        t_title.setObjectName("cardTitle")
        c_layout.addWidget(t_title)

        self.tg_summary_label = QLabel()
        self.tg_summary_label.setObjectName("valueLabel")
        self.tg_summary_label.setWordWrap(True)
        c_layout.addWidget(self.tg_summary_label)

        hint = QLabel(
            "TDLib 使用官方 API ID 和 API Hash 连接 Telegram 核心。\n"
            "如需通过 SOCKS5、HTTP 或 MTProto 代理连接，请点击下方按钮配置代理参数。"
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        c_layout.addWidget(hint)

        btn = QPushButton("打开配置编辑器以修改 Telegram 参数")
        btn.setObjectName("secondaryButton")
        btn.setIcon(get_svg_icon("telegram", 14, 14))
        btn.clicked.connect(self.open_editor)
        self.theme_icon_buttons.append((btn, "telegram"))
        c_layout.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)

        p_layout.addWidget(card)
        p_layout.addStretch(1)
        self.stack.addWidget(page)

    def _build_upload_tab(self):
        page = QWidget()
        p_layout = QVBoxLayout(page)
        p_layout.setContentsMargins(0, 0, 0, 0)
        p_layout.setSpacing(16)

        card = QFrame()
        card.setObjectName("surfaceCard")
        c_layout = QVBoxLayout(card)
        c_layout.setContentsMargins(18, 16, 18, 16)
        c_layout.setSpacing(12)

        u_title = QLabel("媒体上传与相册分组策略")
        u_title.setObjectName("cardTitle")
        c_layout.addWidget(u_title)

        info = QLabel(
            "• 相册聚合：支持 2-10 个媒体文件自动合并为单一 Telegram 相册，保持原始宽高比。\n"
            "• 暂存保护：大文件上传时支持自动暂存，防止源文件在上传途中被外部程序修改。\n"
            "• 标题文本：支持外部 .txt 标题文件匹配，遵循 Telegram 1024 字符/2048 字符软限制。\n"
            "• 目标配置：各分类独立保存目标频道 Chat ID 与子话题 Topic ID。"
        )
        info.setObjectName("valueLabel")
        info.setWordWrap(True)
        c_layout.addWidget(info)

        btn_row = QHBoxLayout()
        btn = QPushButton("编辑上传与暂存配置")
        btn.setObjectName("secondaryButton")
        btn.setIcon(get_svg_icon("edit", 14, 14))
        btn.clicked.connect(self.open_editor)
        self.theme_icon_buttons.append((btn, "edit"))
        btn_row.addWidget(btn)
        btn_row.addStretch(1)
        c_layout.addLayout(btn_row)

        p_layout.addWidget(card)
        p_layout.addStretch(1)
        self.stack.addWidget(page)

    def _build_scan_tools_tab(self):
        page = QWidget()
        p_layout = QVBoxLayout(page)
        p_layout.setContentsMargins(0, 0, 0, 0)
        p_layout.setSpacing(16)

        card = QFrame()
        card.setObjectName("surfaceCard")
        c_layout = QVBoxLayout(card)
        c_layout.setContentsMargins(18, 16, 18, 16)
        c_layout.setSpacing(12)

        s_title = QLabel("扫描稳定性与外部工具参数")
        s_title.setObjectName("cardTitle")
        c_layout.addWidget(s_title)

        desc = QLabel(
            "管理文件稳定窗口等待时间（防止正在写入的文件被扫描上传）、并发扫描线程数、"
            "ExifTool 路径以及缩略图提取核心的配置。"
        )
        desc.setObjectName("valueLabel")
        desc.setWordWrap(True)
        c_layout.addWidget(desc)

        btn_row = QHBoxLayout()
        btn = QPushButton("打开扫描与工具详细配置弹窗")
        btn.setObjectName("secondaryButton")
        btn.setIcon(get_svg_icon("search", 14, 14))
        btn.clicked.connect(self.open_scan_tools)
        self.theme_icon_buttons.append((btn, "search"))
        btn_row.addWidget(btn)
        btn_row.addStretch(1)
        c_layout.addLayout(btn_row)

        p_layout.addWidget(card)
        p_layout.addStretch(1)
        self.stack.addWidget(page)

    def _build_storage_tab(self):
        page = QWidget()
        p_layout = QVBoxLayout(page)
        p_layout.setContentsMargins(0, 0, 0, 0)
        p_layout.setSpacing(16)

        # Diagnostic Data & Logs Row (Exact QHBoxLayout layout contract for test_regression_matrix)
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

        p_layout.addWidget(diag_widget)

        # Cache Management Card
        cache_card = QFrame()
        cache_card.setObjectName("surfaceCard")
        cache_layout = QVBoxLayout(cache_card)
        cache_layout.setContentsMargins(18, 16, 18, 16)
        cache_layout.setSpacing(12)

        c_title = QLabel("存储与缓存维护")
        c_title.setObjectName("cardTitle")
        cache_layout.addWidget(c_title)

        self._cache_worker = None
        self.cache_status = QLabel("就绪，点击“统计缓存占用”可统计占用空间")
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
        calc_cache = QPushButton("统计缓存占用")
        calc_cache.setObjectName("secondaryButton")
        calc_cache.setIcon(get_svg_icon("refresh", 14, 14))
        calc_cache.clicked.connect(self.refresh_cache_stats)
        self.theme_icon_buttons.append((calc_cache, "refresh"))

        clear_thumb = QPushButton("仅清理视频封面缓存")
        clear_thumb.setObjectName("secondaryButton")
        clear_thumb.setIcon(get_svg_icon("delete", 14, 14))
        clear_thumb.clicked.connect(lambda: self.clear_thumb_requested.emit())
        self.theme_icon_buttons.append((clear_thumb, "delete"))

        clear_all = QPushButton("清理所有缓存与断点")
        clear_all.setObjectName("dangerButton")
        clear_all.setIcon(get_svg_icon("delete", 14, 14, color="#ffffff"))
        clear_all.clicked.connect(lambda: self.clear_all_requested.emit())

        cache_buttons.addWidget(calc_cache)
        cache_buttons.addWidget(clear_thumb)
        cache_buttons.addWidget(clear_all)
        cache_buttons.addStretch(1)
        cache_layout.addLayout(cache_buttons)

        p_layout.addWidget(cache_card)
        p_layout.addStretch(1)
        self.stack.addWidget(page)

    def _build_diagnostics_tab(self):
        page = QWidget()
        p_layout = QVBoxLayout(page)
        p_layout.setContentsMargins(0, 0, 0, 0)
        p_layout.setSpacing(16)

        diag_card = QFrame()
        diag_card.setObjectName("surfaceCard")
        d_layout = QVBoxLayout(diag_card)
        d_layout.setContentsMargins(18, 16, 18, 16)
        d_layout.setSpacing(14)

        d_title = QLabel("运行环境与核心依赖检测")
        d_title.setObjectName("cardTitle")
        d_layout.addWidget(d_title)

        env_grid = QGridLayout()
        env_grid.setSpacing(12)
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
        p_layout.addWidget(diag_card)
        p_layout.addStretch(1)
        self.stack.addWidget(page)

    def _build_about_tab(self):
        page = QWidget()
        p_layout = QVBoxLayout(page)
        p_layout.setContentsMargins(0, 0, 0, 0)
        p_layout.setSpacing(16)

        card = QFrame()
        card.setObjectName("surfaceCard")
        c_layout = QVBoxLayout(card)
        c_layout.setContentsMargins(18, 16, 18, 16)
        c_layout.setSpacing(12)

        title = QLabel("关于 TDLib Media Uploader")
        title.setObjectName("cardTitle")
        c_layout.addWidget(title)

        version = read_version()
        ver_label = QLabel(f"当前版本：{version}")
        ver_label.setObjectName("valueLabel")
        c_layout.addWidget(ver_label)

        license_hint = QLabel(
            "TDLib Media Uploader 是一款现代高效的 Telegram 批量媒体上传工具。\n"
            "原创代码采用 GNU General Public License v3.0 only（GPL-3.0-only）授权开源。\n"
            "TDLib、Qt/PySide6、Pillow、FFmpeg、PyInstaller 遵循各自开源许可协议，详见 THIRD_PARTY_LICENSES.md。"
        )
        license_hint.setObjectName("mutedLabel")
        license_hint.setWordWrap(True)
        c_layout.addWidget(license_hint)

        p_layout.addWidget(card)
        p_layout.addStretch(1)
        self.stack.addWidget(page)

    def refresh_cache_stats(self):
        """Asynchronously compute cache size on a worker thread to avoid freezing UI."""
        if self._cache_worker is not None and self._cache_worker.isRunning():
            return
        self.cache_status.setText("正在计算缓存占用…")
        from ..workers import CacheStatsWorker  # noqa: PLC0415

        self._cache_worker = CacheStatsWorker(self)
        self._cache_worker.finished.connect(self._on_cache_stats_ready)
        self._cache_worker.start()

    def _on_cache_stats_ready(self, text: str):
        self.cache_status.setText(text)

    def refresh_theme(self):
        """Re-apply dynamic styles when theme is toggled."""
        for idx, (_, icon_name) in enumerate(self.SETTINGS_CATEGORIES):
            item = self.nav_list.item(idx)
            if item is not None:
                item.setIcon(get_svg_icon(icon_name, 16, 16))
        for btn, icon_name in self.theme_icon_buttons:
            btn.setIcon(get_svg_icon(icon_name, 14, 14))
        self.refresh()

    def refresh(self, update_cache: bool = False):
        from ..config_service import _CONFIG_ERROR, get_config

        if _CONFIG_ERROR:
            self.config_status.setText(f"配置不可用：{_CONFIG_ERROR}")
            self.config_status.setProperty("status", "danger")
        elif _cfg("API_ID", 12345678) == 12345678 or _cfg("API_HASH", "YOUR_API_HASH") == "YOUR_API_HASH":
            self.config_status.setText("配置文件已找到，但 Telegram API 仍为示例值，请先编辑配置。")
            self.config_status.setProperty("status", "warning")
        else:
            self.config_status.setText(f"配置文件已加载：{CONFIG_PATH}")
            self.config_status.setProperty("status", "success")
        self.config_status.style().unpolish(self.config_status)
        self.config_status.style().polish(self.config_status)

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

        # Telegram tab summary
        if hasattr(self, "tg_summary_label"):
            api_id = _cfg("API_ID", 12345678)
            api_configured = (api_id != 12345678 and str(api_id).strip() != "")
            proxy_enabled = _cfg("PROXY_ENABLED", False)
            self.tg_summary_label.setText(
                f"• API 凭据：{'已配置' if api_configured else '使用默认示例值（需配置）'}\n"
                f"• 网络模式：{'代理专线连接' if proxy_enabled else '直连模式'}"
            )

        if update_cache:
            self.refresh_cache_stats()


__all__ = ["SettingsPage"]
