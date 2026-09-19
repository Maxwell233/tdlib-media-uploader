# -*- coding: utf-8 -*-
"""Modern design system, color tokens, and stylesheet for TDLib Media Uploader Beta 4."""

from __future__ import annotations

import sys
from typing import NamedTuple


class Palette(NamedTuple):
    # Backgrounds
    bg_base: str = "#0b0f17"
    bg_sidebar: str = "#080c12"
    bg_surface: str = "#111722"
    bg_card: str = "#161f2e"
    bg_card_hover: str = "#1c283c"
    bg_input: str = "#0d131d"
    bg_selection: str = "#1a365d"
    bg_alt_row: str = "#121926"

    # Borders
    border_subtle: str = "#1e293b"
    border_card: str = "#243247"
    border_hover: str = "#33455e"
    border_focus: str = "#38bdf8"

    # Text
    text_primary: str = "#f8fafc"
    text_secondary: str = "#cbd5e1"
    text_muted: str = "#8192a6"
    text_dim: str = "#475569"

    # Accents & Semantics
    accent: str = "#0284c7"
    accent_hover: str = "#0ea5e9"
    accent_active: str = "#0369a1"

    text_selection: str = "#f8fafc"

    success: str = "#10b981"
    success_hover: str = "#059669"
    success_bg: str = "#064e3b"

    warning: str = "#f59e0b"
    warning_hover: str = "#d97706"
    warning_bg: str = "#78350f"

    danger: str = "#ef4444"
    danger_hover: str = "#dc2626"
    danger_bg: str = "#7f1d1d"

    info: str = "#6366f1"
    info_hover: str = "#4f46e5"
    info_bg: str = "#312e81"


DARK_PALETTE = Palette()

LIGHT_PALETTE = Palette(
    # Backgrounds
    bg_base="#f8fafc",
    bg_sidebar="#f1f5f9",
    bg_surface="#ffffff",
    bg_card="#ffffff",
    bg_card_hover="#f8fafc",
    bg_input="#ffffff",
    bg_selection="#bae6fd",
    bg_alt_row="#f8fafc",

    # Borders
    border_subtle="#e2e8f0",
    border_card="#cbd5e1",
    border_hover="#94a3b8",
    border_focus="#0284c7",

    # Text
    text_primary="#0f172a",
    text_secondary="#334155",
    text_muted="#64748b",
    text_dim="#94a3b8",

    # Accents & Semantics
    accent="#0284c7",
    accent_hover="#0ea5e9",
    accent_active="#0369a1",
    text_selection="#0369a1",

    success="#10b981",
    success_hover="#059669",
    success_bg="#dcfce7",

    warning="#f59e0b",
    warning_hover="#d97706",
    warning_bg="#fef3c7",

    danger="#ef4444",
    danger_hover="#dc2626",
    danger_bg="#fee2e2",

    info="#6366f1",
    info_hover="#4f46e5",
    info_bg="#e0e7ff",
)


class DynamicTheme(Palette):
    """Dynamic theme proxy that allows runtime light/dark switching while remaining a Palette instance."""

    def __init__(self, default_mode: str = "dark"):
        super().__init__()
        object.__setattr__(self, "_mode", "dark")
        object.__setattr__(self, "_palette", DARK_PALETTE)
        if default_mode != "dark":
            self.set_mode(default_mode)

    @property
    def mode(self) -> str:
        return object.__getattribute__(self, "_mode")

    @property
    def current(self) -> Palette:
        return object.__getattribute__(self, "_palette")

    def set_mode(self, mode: str) -> str:
        norm = "light" if str(mode).lower() in ("light", "white", "亮色", "浅色") else "dark"
        object.__setattr__(self, "_mode", norm)
        object.__setattr__(self, "_palette", LIGHT_PALETTE if norm == "light" else DARK_PALETTE)
        return norm

    def toggle(self) -> str:
        new_mode = "light" if self.mode == "dark" else "dark"
        return self.set_mode(new_mode)

    def __getattribute__(self, name: str):
        if name in (
            "_mode",
            "_palette",
            "mode",
            "current",
            "set_mode",
            "toggle",
            "__class__",
            "__dict__",
            "__repr__",
        ):
            return object.__getattribute__(self, name)
        curr = object.__getattribute__(self, "_palette")
        if hasattr(curr, name):
            return getattr(curr, name)
        return object.__getattribute__(self, name)

    def __repr__(self) -> str:
        return f"<DynamicTheme mode={self.mode!r}>"


THEME = DynamicTheme()


def get_current_theme_mode() -> str:
    """Return the current theme mode string ('dark' or 'light')."""
    return THEME.mode


def set_theme_mode(mode: str) -> str:
    """Set the theme mode ('dark' or 'light') and return normalized mode."""
    return THEME.set_mode(mode)


def toggle_theme() -> str:
    """Toggle between dark and light theme modes and return the new mode."""
    return THEME.toggle()


FONT_FAMILY = (
    '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", "PingFang SC", "Microsoft YaHei UI", sans-serif'
    if sys.platform == "darwin"
    else '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
)


def build_stylesheet(palette: Palette | str = THEME) -> str:
    """Generate the complete Qt Stylesheet for Beta 4."""
    if isinstance(palette, str):
        palette = LIGHT_PALETTE if palette.lower() == "light" else DARK_PALETTE
    return f"""
QMainWindow, QWidget {{
    background-color: {palette.bg_base};
    color: {palette.text_secondary};
    font-family: {FONT_FAMILY};
    font-size: 13px;
}}

QToolTip {{
    background-color: {palette.bg_card};
    color: {palette.text_primary};
    border: 1px solid {palette.border_card};
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
}}

/* Sidebar navigation */
QFrame#sidebarFrame {{
    background-color: {palette.bg_sidebar};
    border: 0;
    border-right: 1px solid {palette.border_subtle};
}}

QListWidget#sidebar {{
    background-color: transparent;
    border: 0;
    padding: 10px 8px;
    outline: 0;
}}

QListWidget#sidebar::item {{
    padding: 10px 14px;
    margin: 2px 0;
    border-radius: 8px;
    color: {palette.text_muted};
    font-weight: 500;
}}

QListWidget#sidebar::item:hover {{
    background-color: {palette.bg_card};
    color: {palette.text_primary};
}}

QListWidget#sidebar::item:selected {{
    background-color: {palette.accent};
    color: #ffffff;
    font-weight: 600;
}}

QLabel#sidebarAppTitle {{
    font-size: 16px;
    font-weight: 700;
    color: {palette.text_primary};
}}

QLabel#sidebarBadge {{
    background-color: {palette.bg_selection};
    color: {palette.text_selection};
    font-size: 10px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 6px;
}}

QLabel#sidebarSubtitle {{
    font-size: 11px;
    color: {palette.text_dim};
}}

QFrame#sidebarStatusBox {{
    background-color: {palette.bg_surface};
    border: 1px solid {palette.border_subtle};
    border-radius: 8px;
    padding: 6px 10px;
}}

QLabel#sidebarStatusDot {{
    font-size: 10px;
    color: {palette.text_dim};
}}

QLabel#sidebarStatusDot[connected="true"] {{
    color: {palette.success};
}}

QLabel#sidebarStatusDot[connected="warn"] {{
    color: {palette.warning};
}}

QLabel#sidebarStatusDot[connected="false"] {{
    color: {palette.text_dim};
}}

QLabel#sidebarStatusText {{
    color: {palette.text_muted};
    font-size: 11px;
    font-weight: 500;
}}

QLabel#sidebarVersionLabel {{
    color: {palette.text_dim};
    font-size: 10px;
}}

/* Group Boxes & Cards */
QGroupBox {{
    background-color: {palette.bg_surface};
    border: 1px solid {palette.border_subtle};
    border-radius: 10px;
    margin-top: 14px;
    padding: 16px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 8px;
    color: {palette.text_primary};
    background-color: {palette.bg_surface};
    border-radius: 4px;
}}

QFrame#statCard {{
    background-color: {palette.bg_card};
    border: 1px solid {palette.border_subtle};
    border-radius: 10px;
}}

QFrame#statCard:hover {{
    border-color: {palette.border_card};
}}

QFrame#bannerFrame {{
    background-color: {palette.bg_surface};
    border: 1px solid {palette.border_subtle};
    border-radius: 10px;
    padding: 12px 16px;
}}

/* Typography */
QLabel {{
    background: transparent;
}}

QLabel#pageTitle {{
    font-size: 22px;
    font-weight: 700;
    color: {palette.text_primary};
}}

QLabel#sectionTitle {{
    font-size: 15px;
    font-weight: 600;
    color: {palette.text_primary};
}}

QLabel#statValue {{
    font-size: 22px;
    font-weight: 700;
    color: {palette.text_primary};
}}

QLabel#statValue[good="true"] {{
    color: {palette.success};
}}

QLabel#statValue[good="false"] {{
    color: {palette.warning};
}}

QLabel#valueLabel {{
    color: {palette.text_primary};
    font-weight: 600;
}}

QLabel#mutedLabel {{
    color: {palette.text_muted};
    font-size: 12px;
}}

QLabel#badgeLabel {{
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 10px;
}}

/* ActionCard components */
QLabel#actionCardIcon {{
    font-size: 18px;
    color: {palette.accent};
}}

QLabel#actionCardBadge {{
    background-color: {palette.bg_selection};
    color: {palette.text_selection};
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 8px;
}}

/* Environment & Settings cards */
QFrame#envCard {{
    background-color: {palette.bg_card};
    border: 1px solid {palette.border_subtle};
    border-radius: 8px;
    padding: 10px 12px;
}}

QFrame#envCard:hover {{
    border-color: {palette.border_card};
}}

QLabel#configStatus[status="danger"] {{
    color: {palette.danger};
    font-weight: 600;
}}

QLabel#configStatus[status="warning"] {{
    color: {palette.warning};
    font-weight: 600;
}}

QLabel#configStatus[status="success"] {{
    color: {palette.success};
    font-weight: 600;
}}

QLabel#envStatus[status="ok"] {{
    color: {palette.success};
    font-weight: 600;
}}

QLabel#envStatus[status="warn"] {{
    color: {palette.warning};
    font-weight: 600;
}}

QLabel#envStatus[status="muted"] {{
    color: {palette.text_muted};
}}

QLabel#captionCount[over_limit="true"] {{
    color: {palette.danger};
    font-weight: 600;
}}

QLabel#captionCount[over_limit="false"] {{
    color: {palette.text_muted};
}}

/* Status Pill semantic states */
QLabel#statusPill {{
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}}

QLabel#statusPill[status="success"] {{
    color: {palette.success};
    background-color: {palette.success_bg};
    border: 1px solid {palette.success}40;
}}

QLabel#statusPill[status="warning"] {{
    color: {palette.warning};
    background-color: {palette.warning_bg};
    border: 1px solid {palette.warning}40;
}}

QLabel#statusPill[status="danger"] {{
    color: {palette.danger};
    background-color: {palette.danger_bg};
    border: 1px solid {palette.danger}40;
}}

QLabel#statusPill[status="info"] {{
    color: {palette.info};
    background-color: {palette.info_bg};
    border: 1px solid {palette.info}40;
}}

QLabel#statusPill[status="neutral"] {{
    color: {palette.text_muted};
    background-color: {palette.bg_surface};
    border: 1px solid {palette.border_subtle};
}}

QLabel#statusPill[status="accent"] {{
    color: {palette.accent_hover};
    background-color: {palette.bg_selection};
    border: 1px solid {palette.accent}40;
}}

/* Segmented Control */
QFrame#segmentedFrame {{
    background-color: {palette.bg_surface};
    border: 1px solid {palette.border_subtle};
    border-radius: 8px;
    padding: 3px;
}}

QPushButton#segmentedButton {{
    background-color: transparent;
    border: none;
    border-radius: 6px;
    color: {palette.text_secondary};
    font-weight: 500;
    padding: 6px 14px;
    min-height: 28px;
}}

QPushButton#segmentedButton:hover {{
    background-color: {palette.bg_card_hover};
    color: {palette.text_primary};
}}

QPushButton#segmentedButton[active="true"], QPushButton#segmentedButton:checked {{
    background-color: {palette.accent};
    color: #ffffff;
    font-weight: 600;
}}

/* Modern Surfaces and Metric Chips */
QFrame#surfaceCard {{
    background-color: {palette.bg_card};
    border: 1px solid {palette.border_subtle};
    border-radius: 10px;
    padding: 14px 18px;
}}

QFrame#surfaceCard:hover {{
    border-color: {palette.border_card};
}}

QFrame#metricChip {{
    background-color: {palette.bg_surface};
    border: 1px solid {palette.border_subtle};
    border-radius: 8px;
    padding: 8px 12px;
}}

QLabel#metricChipValue {{
    font-size: 15px;
    font-weight: 700;
    color: {palette.text_primary};
}}

QLabel#metricChipLabel {{
    font-size: 11px;
    color: {palette.text_muted};
}}

/* Settings Navigation List */
QListWidget#settingsNav {{
    background-color: {palette.bg_surface};
    border: 1px solid {palette.border_subtle};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}

QListWidget#settingsNav::item {{
    border-radius: 6px;
    padding: 8px 12px;
    color: {palette.text_secondary};
    font-weight: 500;
    min-height: 24px;
}}

QListWidget#settingsNav::item:hover {{
    background-color: {palette.bg_card_hover};
    color: {palette.text_primary};
}}

QListWidget#settingsNav::item:selected {{
    background-color: {palette.bg_selection};
    color: {palette.text_selection};
    font-weight: 600;
}}

/* Buttons */
QPushButton {{
    min-height: 32px;
    padding: 0 16px;
    border-radius: 7px;
    border: 1px solid {palette.border_subtle};
    background-color: {palette.bg_card};
    color: {palette.text_secondary};
    font-weight: 500;
}}

QPushButton:hover {{
    background-color: {palette.bg_card_hover};
    border-color: {palette.border_hover};
    color: {palette.text_primary};
}}

QPushButton:pressed {{
    background-color: {palette.bg_input};
}}

QPushButton:disabled {{
    color: {palette.text_dim};
    background-color: {palette.bg_surface};
    border-color: {palette.border_subtle};
}}

QPushButton#primaryButton {{
    background-color: {palette.accent};
    border-color: {palette.accent_hover};
    color: #ffffff;
    font-weight: 600;
}}

QPushButton#primaryButton:hover {{
    background-color: {palette.accent_hover};
    border-color: #7dd3fc;
}}

QPushButton#primaryButton:pressed {{
    background-color: {palette.accent_active};
}}

QPushButton#secondaryButton {{
    background-color: {palette.bg_card};
    border: 1px solid {palette.border_card};
    color: {palette.text_primary};
}}

QPushButton#secondaryButton:hover {{
    background-color: {palette.bg_card_hover};
    border-color: {palette.border_hover};
}}

QPushButton#successButton {{
    background-color: {palette.success};
    border-color: {palette.success_hover};
    color: #ffffff;
    font-weight: 600;
}}

QPushButton#successButton:hover {{
    background-color: {palette.success_hover};
}}

QPushButton#dangerButton {{
    background-color: {palette.danger};
    border-color: {palette.danger_hover};
    color: #ffffff;
    font-weight: 600;
}}

QPushButton#dangerButton:hover {{
    background-color: {palette.danger_hover};
}}

QPushButton#ghostButton {{
    background-color: transparent;
    border: 1px solid transparent;
    color: {palette.text_muted};
}}

QPushButton#ghostButton:hover {{
    background-color: {palette.bg_card};
    color: {palette.text_primary};
}}

/* Form Inputs */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background-color: {palette.bg_input};
    color: {palette.text_primary};
    border: 1px solid {palette.border_subtle};
    border-radius: 7px;
    padding: 6px 10px;
    selection-background-color: {palette.bg_selection};
    selection-color: {palette.text_selection};
}}

QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {palette.border_focus};
}}

/* Hide SpinBox / DoubleSpinBox up/down buttons */
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 0px;
    height: 0px;
    border: none;
    background: transparent;
}}

QSpinBox::up-arrow, QSpinBox::down-arrow,
QDoubleSpinBox::up-arrow, QDoubleSpinBox::down-arrow {{
    image: none;
    width: 0px;
    height: 0px;
}}

QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border-left: 0;
}}

QComboBox QAbstractItemView {{
    background-color: {palette.bg_surface};
    color: {palette.text_secondary};
    border: 1px solid {palette.border_card};
    border-radius: 6px;
    padding: 4px;
    selection-background-color: {palette.bg_selection};
    selection-color: {palette.text_selection};
    outline: 0;
}}

/* Checkboxes */
QCheckBox {{
    spacing: 8px;
    color: {palette.text_secondary};
}}

QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid {palette.border_subtle};
    background-color: {palette.bg_input};
}}

QCheckBox::indicator:hover {{
    border-color: {palette.border_hover};
}}

QCheckBox::indicator:checked {{
    background-color: {palette.accent};
    border-color: {palette.accent_hover};
}}

/* Tree & Table */
QTreeWidget, QTableWidget {{
    background-color: {palette.bg_input};
    color: {palette.text_secondary};
    border: 1px solid {palette.border_subtle};
    border-radius: 8px;
    padding: 4px;
    alternate-background-color: {palette.bg_alt_row};
    gridline-color: {palette.border_subtle};
    selection-background-color: {palette.bg_selection};
    selection-color: {palette.text_selection};
    outline: 0;
}}

QTreeWidget:focus, QTableWidget:focus {{
    border-color: {palette.border_hover};
}}

QTreeWidget::item, QTableWidget::item {{
    padding: 6px 8px;
    border-radius: 4px;
}}

QTreeWidget::item:hover, QTableWidget::item:hover {{
    background-color: {palette.bg_card_hover};
    color: {palette.text_primary};
}}

QTreeWidget::item:selected, QTableWidget::item:selected {{
    background-color: {palette.bg_selection};
    color: {palette.text_selection};
    font-weight: 500;
}}

QHeaderView::section {{
    background-color: {palette.bg_card};
    color: {palette.text_muted};
    border: 0;
    border-bottom: 1px solid {palette.border_subtle};
    padding: 8px 10px;
    font-weight: 600;
    font-size: 12px;
}}

QTableCornerButton::section {{
    background-color: {palette.bg_card};
    border: 0;
}}

/* Progress Bar */
QProgressBar {{
    background-color: {palette.bg_input};
    border: 1px solid {palette.border_subtle};
    border-radius: 7px;
    height: 16px;
    text-align: center;
    color: {palette.text_primary};
    font-size: 11px;
    font-weight: 600;
}}

QProgressBar::chunk {{
    background-color: {palette.accent};
    border-radius: 6px;
}}

/* Scroll Bars */
QScrollBar:vertical {{
    background-color: transparent;
    width: 10px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background-color: {palette.border_card};
    border-radius: 5px;
    min-height: 28px;
    margin: 2px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {palette.border_hover};
}}

QScrollBar:horizontal {{
    background-color: transparent;
    height: 10px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background-color: {palette.border_card};
    border-radius: 5px;
    min-width: 28px;
    margin: 2px;
}}

QScrollBar::handle:horizontal:hover {{
    background-color: {palette.border_hover};
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    background: transparent;
    border: 0;
}}

/* Status Bar */
QStatusBar {{
    background-color: {palette.bg_sidebar};
    color: {palette.text_muted};
    border-top: 1px solid {palette.border_subtle};
    padding: 4px 10px;
    font-size: 12px;
}}

/* Menus */
QMenu {{
    background-color: {palette.bg_surface};
    color: {palette.text_secondary};
    border: 1px solid {palette.border_card};
    border-radius: 8px;
    padding: 6px;
}}

QMenu::item {{
    padding: 6px 14px;
    border-radius: 4px;
}}

QMenu::item:selected {{
    background-color: {palette.bg_selection};
    color: {palette.text_selection};
}}

/* Tab Widget */
QTabWidget::pane {{
    border: 1px solid {palette.border_subtle};
    border-radius: 8px;
    background-color: {palette.bg_surface};
    padding: 12px;
}}

QTabBar::tab {{
    background-color: {palette.bg_card};
    color: {palette.text_muted};
    border: 1px solid {palette.border_subtle};
    border-bottom: 0;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 8px 18px;
    margin-right: 4px;
    font-weight: 500;
}}

QTabBar::tab:selected {{
    background-color: {palette.bg_surface};
    color: {palette.text_primary};
    border-color: {palette.border_card};
    font-weight: 600;
}}

QTabBar::tab:hover:!selected {{
    background-color: {palette.bg_card_hover};
    color: {palette.text_secondary};
}}

/* Theme toggle button (Sun / Moon) */
QPushButton#themeToggleBtn {{
    min-height: 28px;
    max-height: 28px;
    min-width: 28px;
    max-width: 28px;
    padding: 0;
    border-radius: 14px;
    background-color: {palette.bg_card};
    border: 1px solid {palette.border_card};
    font-size: 14px;
}}

QPushButton#themeToggleBtn:hover {{
    background-color: {palette.bg_card_hover};
    border-color: {palette.border_hover};
}}
"""


APP_STYLE = build_stylesheet()

__all__ = [
    "THEME",
    "Palette",
    "DARK_PALETTE",
    "LIGHT_PALETTE",
    "DynamicTheme",
    "get_current_theme_mode",
    "set_theme_mode",
    "toggle_theme",
    "APP_STYLE",
    "build_stylesheet",
    "FONT_FAMILY",
]
