# -*- coding: utf-8 -*-
"""Independent configuration dialog for general, path, staging and proxy settings."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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

from ...config.paths import STAGING_CACHE_DIR, read_version
from .. import config_service
from ..config_service import get_cfg as _cfg


class ConfigDialog(QDialog):
    """Modern modal for editing general account, directories, SMB staging and network proxy."""

    def __init__(self, parent=None):
        super().__init__(parent)
        version = read_version()
        self.setWindowTitle(f"编辑基础配置 · V{version}")
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

        form = QFormLayout()
        form.setSpacing(10)
        self.fields = {}

        def field(key: str, value, password=False):
            widget = QLineEdit(str(value if value is not None else ""))
            if password:
                widget.setEchoMode(QLineEdit.EchoMode.Password)
            self.fields[key] = widget
            return widget

        # Telegram Credentials
        cred_box = QGroupBox("1 · Telegram API 凭据")
        cred_form = QFormLayout(cred_box)
        cred_form.setSpacing(10)
        cred_form.addRow("API ID", field("api_id", _cfg("API_ID", 12345678)))
        cred_form.addRow("API Hash", field("api_hash", _cfg("API_HASH", "YOUR_API_HASH"), True))
        content_layout.addWidget(cred_box)

        # Default directories
        dir_box = QGroupBox("2 · 默认媒体目录")
        dir_form = QFormLayout(dir_box)
        dir_form.setSpacing(10)
        dir_form.addRow("视频目录", field("video_dir", _cfg("VIDEO_DIR", "")))
        dir_form.addRow("图片目录", field("image_dir", _cfg("IMAGE_DIR", "")))
        dir_form.addRow("混合目录", field("mixed_dir", _cfg("MIXED_DIR", "")))
        content_layout.addWidget(dir_box)

        # SMB Staging
        stage_box = QGroupBox("3 · 本地暂存（适合网络共享盘 / SMB / NAS）")
        stage_form = QFormLayout(stage_box)
        stage_form.setSpacing(10)

        self.staging_enabled = QCheckBox("启用本地暂存")
        self.staging_enabled.setChecked(bool(_cfg("STAGING_ENABLED", False)))
        stage_form.addRow("暂存开关", self.staging_enabled)

        self.staging_mode = QComboBox()
        self.staging_mode.addItem("关闭（直接读取源文件）", "off")
        self.staging_mode.addItem("仅网络盘", "network")
        self.staging_mode.addItem("所有文件", "always")
        configured_staging_mode = str(
            _cfg("STAGING_MODE", "always" if _cfg("STAGING_ENABLED", False) else "off")
        ).strip().lower()
        mode_index = self.staging_mode.findData(configured_staging_mode)
        self.staging_mode.setCurrentIndex(mode_index if mode_index >= 0 else 0)
        stage_form.addRow("暂存模式", self.staging_mode)

        stage_form.addRow("暂存目录", field("staging_dir", _cfg("STAGING_DIR", STAGING_CACHE_DIR)))

        self.staging_cleanup_on_start = QCheckBox("启动时清理过期暂存文件")
        self.staging_cleanup_on_start.setChecked(
            bool(_cfg("STAGING_CLEANUP_ON_START", True))
        )
        stage_form.addRow("暂存清理", self.staging_cleanup_on_start)

        self.staging_cleanup_days = QSpinBox()
        self.staging_cleanup_days.setRange(0, 3650)
        self.staging_cleanup_days.setValue(int(_cfg("STAGING_CLEANUP_DAYS", 7)))
        self.staging_cleanup_days.setSuffix(" 天")
        stage_form.addRow("暂存保留时间", self.staging_cleanup_days)

        self.staging_cleanup_after_success = QCheckBox("Album 确认成功后删除暂存副本")
        self.staging_cleanup_after_success.setChecked(
            bool(_cfg("STAGING_CLEANUP_AFTER_SUCCESS", True))
        )
        stage_form.addRow("成功后清理", self.staging_cleanup_after_success)

        def sync_legacy_enabled(index):
            enabled = self.staging_mode.itemData(index) != "off"
            self.staging_enabled.blockSignals(True)
            self.staging_enabled.setChecked(enabled)
            self.staging_enabled.blockSignals(False)

        def sync_mode_from_legacy(enabled):
            if enabled and self.staging_mode.currentData() == "off":
                self.staging_mode.setCurrentIndex(self.staging_mode.findData("network"))
            elif not enabled:
                self.staging_mode.setCurrentIndex(self.staging_mode.findData("off"))

        self.staging_mode.currentIndexChanged.connect(sync_legacy_enabled)
        self.staging_enabled.toggled.connect(sync_mode_from_legacy)
        sync_legacy_enabled(self.staging_mode.currentIndex())
        content_layout.addWidget(stage_box)

        # Proxy
        proxy_box = QGroupBox("4 · 网络代理（独立设置，默认关闭）")
        proxy_form = QFormLayout(proxy_box)
        proxy_form.setSpacing(10)

        self.proxy_enabled = QCheckBox("启用网络代理（关闭时直连 Telegram）")
        self.proxy_enabled.setChecked(bool(_cfg("PROXY_ENABLED", False)))
        proxy_form.addRow("代理状态", self.proxy_enabled)

        self.proxy_type = QComboBox()
        self.proxy_type.addItem("SOCKS5", "socks5")
        self.proxy_type.addItem("HTTP", "http")
        self.proxy_type.addItem("MTProto", "mtproto")
        configured_proxy_type = str(_cfg("PROXY_TYPE", "socks5")).lower()
        proxy_index = self.proxy_type.findData(configured_proxy_type)
        self.proxy_type.setCurrentIndex(proxy_index if proxy_index >= 0 else 0)
        proxy_form.addRow("代理类型", self.proxy_type)

        self.proxy_server = field("proxy_server", _cfg("PROXY_SERVER", ""))
        proxy_form.addRow("代理服务器", self.proxy_server)

        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(1, 65535)
        self.proxy_port.setValue(int(_cfg("PROXY_PORT", 1080)))
        proxy_form.addRow("代理端口", self.proxy_port)

        self.proxy_username = field("proxy_username", _cfg("PROXY_USERNAME", ""))
        proxy_username_label = QLabel("代理用户名")
        proxy_form.addRow(proxy_username_label, self.proxy_username)

        self.proxy_password = field("proxy_password", _cfg("PROXY_PASSWORD", ""), True)
        proxy_password_label = QLabel("代理密码")
        proxy_form.addRow(proxy_password_label, self.proxy_password)

        self.proxy_secret = field("proxy_secret", _cfg("PROXY_SECRET", ""), True)
        proxy_secret_label = QLabel("MTProto Secret")
        proxy_form.addRow(proxy_secret_label, self.proxy_secret)

        self.proxy_http_only = QCheckBox("仅支持 HTTP 请求（不支持 CONNECT）")
        self.proxy_http_only.setChecked(bool(_cfg("PROXY_HTTP_ONLY", False)))
        proxy_http_only_label = QLabel("HTTP 选项")
        proxy_form.addRow(proxy_http_only_label, self.proxy_http_only)

        self._proxy_rows = {
            "username": (proxy_username_label, self.proxy_username),
            "password": (proxy_password_label, self.proxy_password),
            "secret": (proxy_secret_label, self.proxy_secret),
            "http_only": (proxy_http_only_label, self.proxy_http_only),
        }
        self.proxy_enabled.toggled.connect(self._update_proxy_fields)
        self.proxy_type.currentIndexChanged.connect(self._update_proxy_fields)
        self._update_proxy_fields()
        content_layout.addWidget(proxy_box)

        hint = QLabel(
            "API Hash、代理认证和 MTProto Secret 仅保存在本地 config.toml 中。\n"
            "启用本地暂存后，发送前会把文件复制到暂存目录，上传断点仍以原始路径为准。"
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        content_layout.addWidget(hint)

        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.setMaximumHeight(max(420, available.height() - 80))
            self.resize(
                min(self.width(), max(620, available.width() - 80)),
                min(self.height(), max(420, available.height() - 80)),
            )

    def _update_proxy_fields(self):
        enabled = self.proxy_enabled.isChecked()
        proxy_type = self.proxy_type.currentData()
        show_credentials = enabled and proxy_type in {"socks5", "http"}
        show_secret = enabled and proxy_type == "mtproto"
        show_http_only = enabled and proxy_type == "http"
        for key in ("username", "password"):
            label, widget = self._proxy_rows[key]
            label.setVisible(show_credentials)
            widget.setVisible(show_credentials)
        label, widget = self._proxy_rows["secret"]
        label.setVisible(show_secret)
        widget.setVisible(show_secret)
        label, widget = self._proxy_rows["http_only"]
        label.setVisible(show_http_only)
        widget.setVisible(show_http_only)
        for key in ("proxy_type", "proxy_server", "proxy_port"):
            getattr(self, key).setEnabled(enabled)
        for row in self._proxy_rows.values():
            for widget in row:
                widget.setEnabled(enabled)

    def _save(self):
        try:
            api_id = int(self.fields["api_id"].text().strip())
            if api_id <= 0:
                raise ValueError
        except ValueError:
            QMessageBox.warning(self, "无法保存", "API ID 必须是正整数。")
            return

        values = {
            ("telegram", "api_id"): api_id,
            ("telegram", "api_hash"): self.fields["api_hash"].text().strip(),
            ("paths", "video_dir"): self.fields["video_dir"].text().strip(),
            ("paths", "image_dir"): self.fields["image_dir"].text().strip(),
            ("paths", "mixed_dir"): self.fields["mixed_dir"].text().strip(),
            ("staging", "enabled"): self.staging_mode.currentData() != "off",
            ("staging", "mode"): self.staging_mode.currentData() or "off",
            ("staging", "directory"): self.fields["staging_dir"].text().strip(),
            ("staging", "cleanup_on_start"): self.staging_cleanup_on_start.isChecked(),
            ("staging", "cleanup_days"): self.staging_cleanup_days.value(),
            ("staging", "cleanup_after_success"): self.staging_cleanup_after_success.isChecked(),
            ("proxy", "enabled"): self.proxy_enabled.isChecked(),
            ("proxy", "type"): self.proxy_type.currentData() or "socks5",
            ("proxy", "server"): self.proxy_server.text().strip(),
            ("proxy", "port"): self.proxy_port.value(),
            ("proxy", "username"): self.proxy_username.text(),
            ("proxy", "password"): self.proxy_password.text(),
            ("proxy", "secret"): self.proxy_secret.text().strip(),
            ("proxy", "http_only"): self.proxy_http_only.isChecked(),
        }
        if values[("proxy", "enabled")]:
            if not values[("proxy", "server")]:
                QMessageBox.critical(self, "保存失败", "启用代理时必须填写代理服务器。")
                return
            if values[("proxy", "type")] == "mtproto" and not values[("proxy", "secret")]:
                QMessageBox.critical(self, "保存失败", "使用 MTProto 代理时必须填写 Secret。")
                return
        error = config_service.write_config_values(values)
        if error:
            QMessageBox.critical(self, "保存失败", error)
            return
        self.accept()


__all__ = ["ConfigDialog"]
