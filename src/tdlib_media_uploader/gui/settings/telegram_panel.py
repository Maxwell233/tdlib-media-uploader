# -*- coding: utf-8 -*-
"""Telegram credentials and proxy configuration panel."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config_service import get_cfg as _cfg
from ..icons import get_svg_icon
from .base import SettingsPanel


class TelegramPanel(SettingsPanel):
    """Panel managing Telegram API ID/Hash and network proxy settings."""

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

        # 1. Telegram API Credentials Card
        cred_card, cred_layout = self.create_card("Telegram API 凭据")
        cred_form = QFormLayout()
        cred_form.setSpacing(10)

        self.api_id = QLineEdit()
        self.api_id.setPlaceholderText("例如 12345678")
        cred_form.addRow("API ID", self.api_id)

        self.api_hash = QLineEdit()
        self.api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_hash.setPlaceholderText("例如 0123456789abcdef0123456789abcdef")

        api_hash_row, self.toggle_hash_btn = self._make_password_row(self.api_hash)
        cred_form.addRow("API Hash", api_hash_row)

        helper_label = QLabel(
            '获取官方 API ID 与 API Hash：'
            '<a href="https://my.telegram.org">my.telegram.org</a>'
        )
        helper_label.setObjectName("mutedLabel")
        helper_label.setOpenExternalLinks(True)
        helper_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

        cred_layout.addLayout(cred_form)
        cred_layout.addWidget(helper_label)
        layout.addWidget(cred_card)

        # 2. Network Proxy Card
        proxy_card, proxy_layout = self.create_card("网络专线与代理")
        proxy_form = QFormLayout()
        proxy_form.setSpacing(10)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(14)
        self.mode_group = QButtonGroup(self)
        self.radio_direct = QRadioButton("直连 Telegram")
        self.radio_proxy = QRadioButton("使用网络代理")
        self.mode_group.addButton(self.radio_direct, 0)
        self.mode_group.addButton(self.radio_proxy, 1)
        mode_row.addWidget(self.radio_direct)
        mode_row.addWidget(self.radio_proxy)
        mode_row.addStretch(1)
        proxy_form.addRow("连接方式", mode_row)

        self.proxy_type = QComboBox()
        self.proxy_type.addItem("SOCKS5", "socks5")
        self.proxy_type.addItem("HTTP", "http")
        self.proxy_type.addItem("MTProto", "mtproto")
        self.proxy_type_label = QLabel("代理类型")
        proxy_form.addRow(self.proxy_type_label, self.proxy_type)

        self.proxy_server = QLineEdit()
        self.proxy_server.setPlaceholderText("例如 127.0.0.1 或 proxy.example.com")
        self.proxy_server_label = QLabel("代理服务器")
        proxy_form.addRow(self.proxy_server_label, self.proxy_server)

        self.proxy_port = QSpinBox()
        self.proxy_port.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.proxy_port.setRange(1, 65535)
        self.proxy_port.setValue(1080)
        self.proxy_port_label = QLabel("代理端口")
        proxy_form.addRow(self.proxy_port_label, self.proxy_port)

        self.proxy_username = QLineEdit()
        self.proxy_username.setPlaceholderText("选填")
        self.proxy_username_label = QLabel("代理用户名")
        proxy_form.addRow(self.proxy_username_label, self.proxy_username)

        self.proxy_password = QLineEdit()
        self.proxy_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_password.setPlaceholderText("选填")
        pwd_row, self.toggle_pwd_btn = self._make_password_row(self.proxy_password)
        self.proxy_password_label = QLabel("代理密码")
        self.proxy_password_row_widget = QWidget()
        pwd_inner = QHBoxLayout(self.proxy_password_row_widget)
        pwd_inner.setContentsMargins(0, 0, 0, 0)
        pwd_inner.addLayout(pwd_row)
        proxy_form.addRow(self.proxy_password_label, self.proxy_password_row_widget)

        self.proxy_secret = QLineEdit()
        self.proxy_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_secret.setPlaceholderText("16 字节十六进制字符或 dd 前缀密钥")
        secret_row, self.toggle_secret_btn = self._make_password_row(self.proxy_secret)
        self.proxy_secret_label = QLabel("MTProto Secret")
        self.proxy_secret_row_widget = QWidget()
        secret_inner = QHBoxLayout(self.proxy_secret_row_widget)
        secret_inner.setContentsMargins(0, 0, 0, 0)
        secret_inner.addLayout(secret_row)
        proxy_form.addRow(self.proxy_secret_label, self.proxy_secret_row_widget)

        self.proxy_http_only = QCheckBox("仅支持 HTTP 请求（不支持 CONNECT 隧道）")
        self.proxy_http_only_label = QLabel("HTTP 选项")
        proxy_form.addRow(self.proxy_http_only_label, self.proxy_http_only)

        proxy_hint = QLabel(
            "API Hash、代理认证凭据与 MTProto Secret 仅安全保存在本地 config.toml 中。"
        )
        proxy_hint.setObjectName("mutedLabel")
        proxy_hint.setWordWrap(True)
        proxy_layout.addLayout(proxy_form)
        proxy_layout.addWidget(proxy_hint)
        layout.addWidget(proxy_card)

        layout.addStretch(1)
        scroll.setWidget(container)
        main_layout.addWidget(scroll)

        # Wire UI visibility changes
        self.radio_direct.toggled.connect(self._update_proxy_visibility)
        self.radio_proxy.toggled.connect(self._update_proxy_visibility)
        self.proxy_type.currentIndexChanged.connect(self._update_proxy_visibility)

        # Wire dirty tracking
        self.api_id.textChanged.connect(self._check_dirty)
        self.api_hash.textChanged.connect(self._check_dirty)
        self.radio_proxy.toggled.connect(self._check_dirty)
        self.proxy_type.currentIndexChanged.connect(self._check_dirty)
        self.proxy_server.textChanged.connect(self._check_dirty)
        self.proxy_port.valueChanged.connect(self._check_dirty)
        self.proxy_username.textChanged.connect(self._check_dirty)
        self.proxy_password.textChanged.connect(self._check_dirty)
        self.proxy_secret.textChanged.connect(self._check_dirty)
        self.proxy_http_only.toggled.connect(self._check_dirty)

        self.load()

    def _make_password_row(self, line_edit: QLineEdit) -> tuple[QHBoxLayout, QPushButton]:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(line_edit, 1)

        btn = QPushButton("显示")
        btn.setObjectName("secondaryButton")
        btn.setIcon(get_svg_icon("eye", 14, 14))
        btn.setCheckable(True)

        def toggle_echo(checked):
            line_edit.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
            btn.setText("隐藏" if checked else "显示")
            btn.setIcon(get_svg_icon("eye-off" if checked else "eye", 14, 14))

        btn.toggled.connect(toggle_echo)
        row.addWidget(btn)
        return row, btn

    def _update_proxy_visibility(self):
        enabled = self.radio_proxy.isChecked()
        p_type = self.proxy_type.currentData() or "socks5"

        for widget in (
            self.proxy_type, self.proxy_type_label,
            self.proxy_server, self.proxy_server_label,
            self.proxy_port, self.proxy_port_label,
        ):
            widget.setVisible(enabled)

        show_user_pwd = enabled and p_type in {"socks5", "http"}
        self.proxy_username.setVisible(show_user_pwd)
        self.proxy_username_label.setVisible(show_user_pwd)
        self.proxy_password_row_widget.setVisible(show_user_pwd)
        self.proxy_password_label.setVisible(show_user_pwd)

        show_secret = enabled and p_type == "mtproto"
        self.proxy_secret_row_widget.setVisible(show_secret)
        self.proxy_secret_label.setVisible(show_secret)

        show_http_only = enabled and p_type == "http"
        self.proxy_http_only.setVisible(show_http_only)
        self.proxy_http_only_label.setVisible(show_http_only)

    def load(self):
        self.blockSignals(True)
        raw_api_id = _cfg("API_ID", 12345678)
        self.api_id.setText(str(raw_api_id if raw_api_id not in (12345678, None, "") else ""))
        raw_hash = _cfg("API_HASH", "YOUR_API_HASH")
        self.api_hash.setText(str(raw_hash if raw_hash not in ("YOUR_API_HASH", None) else ""))

        proxy_enabled = bool(_cfg("PROXY_ENABLED", False))
        self.radio_proxy.setChecked(proxy_enabled)
        self.radio_direct.setChecked(not proxy_enabled)

        p_type = str(_cfg("PROXY_TYPE", "socks5")).lower()
        idx = self.proxy_type.findData(p_type)
        self.proxy_type.setCurrentIndex(idx if idx >= 0 else 0)

        self.proxy_server.setText(str(_cfg("PROXY_SERVER", "")))
        self.proxy_port.setValue(int(_cfg("PROXY_PORT", 1080)))
        self.proxy_username.setText(str(_cfg("PROXY_USERNAME", "")))
        self.proxy_password.setText(str(_cfg("PROXY_PASSWORD", "")))
        self.proxy_secret.setText(str(_cfg("PROXY_SECRET", "")))
        self.proxy_http_only.setChecked(bool(_cfg("PROXY_HTTP_ONLY", False)))

        # Reset password visibility buttons
        for btn, edit in (
            (self.toggle_hash_btn, self.api_hash),
            (self.toggle_pwd_btn, self.proxy_password),
            (self.toggle_secret_btn, self.proxy_secret),
        ):
            btn.setChecked(False)
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            btn.setText("显示")
            btn.setIcon(get_svg_icon("eye", 14, 14))

        self.blockSignals(False)
        self._update_proxy_visibility()

        self._initial_values = self._current_values_dict()
        self.mark_clean()

    def _current_values_dict(self) -> dict:
        return {
            "api_id": self.api_id.text().strip(),
            "api_hash": self.api_hash.text().strip(),
            "proxy_enabled": self.radio_proxy.isChecked(),
            "proxy_type": self.proxy_type.currentData() or "socks5",
            "proxy_server": self.proxy_server.text().strip(),
            "proxy_port": self.proxy_port.value(),
            "proxy_username": self.proxy_username.text(),
            "proxy_password": self.proxy_password.text(),
            "proxy_secret": self.proxy_secret.text().strip(),
            "proxy_http_only": self.proxy_http_only.isChecked(),
        }

    def _check_dirty(self):
        dirty = self._current_values_dict() != self._initial_values
        self.set_dirty(dirty)

    def validate(self) -> tuple[bool, str]:
        text_id = self.api_id.text().strip()
        if not text_id:
            return False, "API ID 不能为空。"
        try:
            val = int(text_id)
            if val <= 0:
                return False, "API ID 必须是正整数。"
        except ValueError:
            return False, "API ID 必须是正整数。"

        if self.radio_proxy.isChecked():
            if not self.proxy_server.text().strip():
                return False, "启用代理时必须填写代理服务器。"
            p_type = self.proxy_type.currentData() or "socks5"
            if p_type == "mtproto" and not self.proxy_secret.text().strip():
                return False, "使用 MTProto 代理时必须填写 Secret。"
        return True, ""

    def collect_values(self) -> dict:
        curr = self._current_values_dict()
        return {
            ("telegram", "api_id"): int(curr["api_id"]),
            ("telegram", "api_hash"): curr["api_hash"],
            ("proxy", "enabled"): curr["proxy_enabled"],
            ("proxy", "type"): curr["proxy_type"],
            ("proxy", "server"): curr["proxy_server"],
            ("proxy", "port"): curr["proxy_port"],
            ("proxy", "username"): curr["proxy_username"],
            ("proxy", "password"): curr["proxy_password"],
            ("proxy", "secret"): curr["proxy_secret"],
            ("proxy", "http_only"): curr["proxy_http_only"],
        }


__all__ = ["TelegramPanel"]
