# -*- coding: utf-8 -*-
"""Modern package-owned media upload page shared by the three media routes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import importlib
import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..dialogs.caption_dialog import CaptionEditDialog
from ...core.album import (
    CaptionLimitError,
    CaptionStore,
    compose_caption,
    validate_caption,
    with_filename_description,
)
from ...core.filesystem_legacy import stable_path
from ...config.paths import RESOURCE_DIR
from ..icons import get_svg_icon
from ..theme import THEME
from ..tools import format_size as _format_size

MEDIA_KINDS = ("video", "image", "mixed")
KIND_LABELS = {"video": "视频", "image": "图片", "mixed": "混合"}
KIND_PATH_KEYS = {"video": "VIDEO_DIR", "image": "IMAGE_DIR", "mixed": "MIXED_DIR"}


def _require_kind(kind: str) -> str:
    normalized = str(kind).strip().lower()
    if normalized not in MEDIA_KINDS:
        raise ValueError(f"未知媒体类型：{kind}")
    return normalized


def _kind_label(kind: str) -> str:
    return KIND_LABELS.get(str(kind).lower(), str(kind))


def _config_getter(name: str, default=None):
    try:
        config = importlib.import_module("tdlib_media_uploader.config.loader")
    except Exception:
        return default
    return getattr(config, name, default)


def _target_getter(kind: str) -> dict[str, Any]:
    try:
        config = importlib.import_module("tdlib_media_uploader.config.loader")
    except Exception:
        config = None
    target_for = getattr(config, "target_for", None)
    if callable(target_for):
        value = target_for(kind)
        return dict(value or {})
    return {
        "target_mode": "forum_topic",
        "group_chat_id": _config_getter("GROUP_CHAT_ID", _config_getter("CHAT_ID", 0)),
        "channel_chat_id": _config_getter("CHANNEL_CHAT_ID", 0),
        "forum_topic_id": _config_getter("FORUM_TOPIC_ID", 0),
        "chat_id": _config_getter("CHAT_ID", 0),
    }


def _path_text(value: Any) -> str:
    return str(value) if value is not None else ""


def _item_path(value: Any) -> Path:
    if isinstance(value, Mapping):
        value = value.get("path")
    return Path(value)


def _item_size(value: Any) -> int:
    path = _item_path(value)
    if isinstance(value, Mapping):
        raw = value.get("scan_size", value.get("size"))
        if raw is not None:
            try:
                return max(0, int(raw))
            except (TypeError, ValueError):
                pass
    try:
        return max(0, int(os.stat(path).st_size))
    except OSError:
        return 0


@dataclass
class UploadPageServices:
    """Dependencies needed to keep the upload page independent of the window."""

    require_kind: Callable[[str], str] = _require_kind
    kind_label: Callable[[str], str] = _kind_label
    config_getter: Callable[[str, Any], Any] = _config_getter
    target_getter: Callable[[str], Mapping[str, Any]] = _target_getter
    path_keys: Mapping[str, str] = field(default_factory=lambda: dict(KIND_PATH_KEYS))
    project_dir: Path = Path(RESOURCE_DIR)
    path_text: Callable[[Any], str] = _path_text
    size_formatter: Callable[[float | int | None], str] = _format_size
    item_size: Callable[[Any], int] = _item_size
    stable_path: Callable[[Any], str] = stable_path
    filename_description: Callable[..., str] = with_filename_description
    compose_caption: Callable[..., str] = compose_caption
    validate_caption: Callable[..., str] = validate_caption
    caption_store_factory: Callable[[str], Any] = CaptionStore
    caption_limit: int = 4096
    dialog_class: Any = QDialog
    message_box_class: Any = QMessageBox
    file_dialog_class: Any = QFileDialog


class UploadPage(QWidget):
    """Modern workbench for media scanning, preview filtering, captioning and upload dispatch."""

    start_requested = Signal(str)
    path_selected = Signal(str, str)
    scan_requested = Signal(str)
    scan_cancel_requested = Signal(str)
    edit_target_requested = Signal(str)
    edit_parameters_requested = Signal(str)

    def __init__(self, kind: str, *, services: UploadPageServices | None = None, parent=None):
        super().__init__(parent)
        self.services = services or UploadPageServices()
        self.kind = self.services.require_kind(kind)
        self.result = None
        self._running = False
        self._scanning = False
        accent = self.services.kind_label(self.kind)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(10)

        # Title & Subtitle Header
        header_layout = QVBoxLayout()
        header_layout.setSpacing(4)
        title = QLabel(f"{accent}上传工作台")
        title.setObjectName("pageTitle")
        header_layout.addWidget(title)

        desc_map = {
            "video": "自动按拍摄月份或固定数量智能分组分发到多个 Album 媒体组；支持 EXIF 与封面提取。",
            "image": "按自然顺序或修改时间分批分组；支持超限自动高质量压缩，确保原生画质发送。",
            "mixed": "以一级子文件夹为分组单元，同组内照片与视频严格保序混合发送为 Album。",
        }
        subtitle = QLabel(desc_map.get(self.kind, "配置媒体来源与目标，一键执行扫描与分批上传。"))
        subtitle.setObjectName("mutedLabel")
        header_layout.addWidget(subtitle)
        layout.addLayout(header_layout)

        # 1. Source & Target Workbench Card (No QGroupBox)
        st_card = QFrame()
        st_card.setObjectName("surfaceCard")
        st_layout = QVBoxLayout(st_card)
        st_layout.setContentsMargins(16, 14, 16, 14)
        st_layout.setSpacing(10)

        # Source directory row
        source_row = QHBoxLayout()
        source_row.setSpacing(10)
        source_lbl = QLabel("本地媒体来源：")
        source_lbl.setObjectName("mutedLabel")
        source_lbl.setFixedWidth(96)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("输入或粘贴媒体目录路径，按 Enter 保存；也可点击右侧浏览选择…")
        self.source_edit.editingFinished.connect(self._commit_source)

        browse = QPushButton("选择目录")
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self._browse)

        source_row.addWidget(source_lbl)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(browse)
        st_layout.addLayout(source_row)

        # Telegram Target row
        target_row = QHBoxLayout()
        target_row.setSpacing(10)
        target_lbl = QLabel("Telegram 目标：")
        target_lbl.setObjectName("mutedLabel")
        target_lbl.setFixedWidth(96)

        self.chat_label = QLabel("未配置")
        self.chat_label.setObjectName("valueLabel")

        topic_tag = QLabel("Topic:")
        topic_tag.setObjectName("mutedLabel")
        self.topic_label = QLabel("未配置")
        self.topic_label.setObjectName("valueLabel")

        edit_target = QPushButton("修改目标")
        edit_target.setObjectName("secondaryButton")
        edit_target.clicked.connect(lambda: self.edit_target_requested.emit(self.kind))

        self.edit_params_button = QPushButton(f"编辑{accent}上传参数")
        self.edit_params_button.setObjectName("secondaryButton")
        self.edit_params_button.setIcon(get_svg_icon("sliders", 14, 14))
        self.edit_params_button.setToolTip(f"跳转至设置页面调整{accent}分卷规则与高级参数")
        self.edit_params_button.clicked.connect(lambda: self.edit_parameters_requested.emit(self.kind))

        target_row.addWidget(target_lbl)
        target_row.addWidget(self.chat_label)
        target_row.addSpacing(14)
        target_row.addWidget(topic_tag)
        target_row.addWidget(self.topic_label)
        target_row.addStretch(1)
        target_row.addWidget(edit_target)
        target_row.addWidget(self.edit_params_button)
        st_layout.addLayout(target_row)

        layout.addWidget(st_card)

        # 2. Scan Summary Metric Chips
        chips_frame = QFrame()
        chips_frame.setObjectName("surfaceCard")
        chips_layout = QHBoxLayout(chips_frame)
        chips_layout.setContentsMargins(12, 10, 12, 10)
        chips_layout.setSpacing(10)

        def make_chip(title_text: str, default_val: str = "—"):
            chip = QFrame()
            chip.setObjectName("metricChip")
            cl = QVBoxLayout(chip)
            cl.setContentsMargins(8, 6, 8, 6)
            cl.setSpacing(2)
            lbl = QLabel(title_text)
            lbl.setObjectName("metricChipLabel")
            val = QLabel(default_val)
            val.setObjectName("metricChipValue")
            cl.addWidget(lbl)
            cl.addWidget(val)
            return chip, val

        c1, self.chip_files = make_chip("文件总数")
        c2, self.chip_bytes = make_chip("数据总量")
        c3, self.chip_done = make_chip("已完成")
        c4, self.chip_pending = make_chip("待上传")
        c5, self.chip_albums = make_chip("Album 媒体组")

        chips_layout.addWidget(c1, 1)
        chips_layout.addWidget(c2, 1)
        chips_layout.addWidget(c3, 1)
        chips_layout.addWidget(c4, 1)
        chips_layout.addWidget(c5, 1)
        layout.addWidget(chips_frame)

        # 3. Preview Section (Visual Hero)
        preview_container = QFrame()
        preview_container.setObjectName("surfaceCard")
        preview_layout = QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(14, 12, 14, 12)
        preview_layout.setSpacing(10)

        # Filter toolbar
        filters = QHBoxLayout()
        filters.setSpacing(10)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索文件名、路径或标题…")
        self.search_edit.setClearButtonEnabled(True)

        self.pending_only = QCheckBox("只看待上传")

        self.edit_caption_button = QPushButton("编辑标题", self)
        self.edit_caption_button.setObjectName("secondaryButton")
        self.edit_caption_button.setIcon(get_svg_icon("edit", 14, 14))
        self.edit_caption_button.setToolTip("编辑所选媒体组标题，也可双击媒体组")
        self.edit_caption_button.setEnabled(False)
        self.edit_caption_button.setVisible(True)
        self.edit_caption_button.clicked.connect(lambda: self._edit_album(self.tree.currentItem()))

        expand_btn = QPushButton("展开全部")
        expand_btn.setObjectName("ghostButton")

        collapse_btn = QPushButton("收起全部")
        collapse_btn.setObjectName("ghostButton")

        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.pending_only)
        filters.addWidget(self.edit_caption_button)
        filters.addWidget(expand_btn)
        filters.addWidget(collapse_btn)
        preview_layout.addLayout(filters)

        # Tree widget
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["状态", "日期 / 分组", "大小", "文件路径"])
        self.tree.header().setStretchLastSection(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setMinimumHeight(150)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        self.tree.itemDoubleClicked.connect(self._edit_album)
        self.tree.itemSelectionChanged.connect(self._update_edit_button)

        expand_btn.clicked.connect(self.tree.expandAll)
        collapse_btn.clicked.connect(self.tree.collapseAll)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self._filter_preview)
        self.search_edit.textChanged.connect(lambda: self.search_timer.start())
        self.pending_only.toggled.connect(self._filter_preview)
        preview_layout.addWidget(self.tree, 1)

        self.summary_label = QLabel("尚未扫描目录")
        self.summary_label.setObjectName("mutedLabel")
        preview_layout.addWidget(self.summary_label)
        layout.addWidget(preview_container, 1)

        # 4. Action & Status Bar
        bottom = QHBoxLayout()
        bottom.setSpacing(12)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("valueLabel")

        self.scan_button = QPushButton("扫描目录")
        self.scan_button.setObjectName("secondaryButton")
        self.scan_button.clicked.connect(self._scan_button_clicked)

        self.start_button = QPushButton("开始上传")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(lambda: self.start_requested.emit(self.kind))

        bottom.addWidget(self.status_label, 1)
        bottom.addWidget(self.scan_button)
        bottom.addWidget(self.start_button)
        layout.addLayout(bottom)

        self.refresh_config()

    def _cfg(self, name: str, default=None):
        return self.services.config_getter(name, default)

    def _size(self, value: Any) -> int:
        return self.services.item_size(value)

    def _stable(self, value: Any) -> str:
        return str(self.services.stable_path(value))

    def refresh_config(self):
        path_name = self.services.path_keys[self.kind]
        self.source_edit.setText(self.services.path_text(self._cfg(path_name, "")))
        target = dict(self.services.target_getter(self.kind) or {})
        mode = str(target.get("target_mode", "forum_topic"))
        target_name = "频道" if mode == "channel" else "超级群组"
        self.chat_label.setText(f"{target_name} · {str(target.get('chat_id', '未配置'))}")
        self.topic_label.setText(
            "不适用（频道不使用 Topic）"
            if mode == "channel"
            else str(target.get("forum_topic_id", "未配置"))
        )

    def _browse(self):
        path = self.services.file_dialog_class.getExistingDirectory(
            self,
            "选择媒体来源目录",
            self.source_edit.text() or str(self.services.project_dir),
        )
        if path:
            self.source_edit.setText(path)
            self.path_selected.emit(self.kind, path)

    def _commit_source(self):
        path = self.source_edit.text().strip()
        saved = str(self._cfg(self.services.path_keys[self.kind], ""))
        if path != saved:
            self.path_selected.emit(self.kind, path)

    def _scan_button_clicked(self):
        if self._scanning:
            self.scan_cancel_requested.emit(self.kind)
        else:
            self.scan_requested.emit(self.kind)

    def set_scanning(self, active: bool):
        self._scanning = active
        self.scan_button.setText("停止扫描" if active else "扫描目录")
        self.scan_button.setEnabled(not self._running)
        if active:
            self.clear_scan_result()
            self.status_label.setText("正在扫描目录…")
        self._update_edit_button()

    def _normalize_legacy_group(self, value: Mapping[str, Any], completed_paths: set[str]) -> dict:
        """Keep older scanner payloads renderable during the V2 migration."""
        group = dict(value or {})
        if all(key in group for key in ("label", "completed", "pending", "albums")):
            return group

        items = list(group.get("items", []) or [])
        pending_items = [
            item
            for item in items
            if self._stable(_item_path(item)) not in completed_paths
        ]
        label = str(group.get("label") or group.get("title") or group.get("subtitle") or "")
        raw_caption = group.get("caption", "")
        if isinstance(raw_caption, Mapping):
            base_label = str(raw_caption.get("base_label", label) or label)
            custom_text = str(raw_caption.get("custom_text", "") or "")
            separator_key = {
                "video": "VIDEO_ALBUM_CAPTION_SEPARATOR",
                "image": "IMAGE_ALBUM_CAPTION_SEPARATOR",
                "mixed": "MIXED_ALBUM_CAPTION_SEPARATOR",
            }[self.kind]
            caption_text = self.services.compose_caption(
                base_label,
                custom_text,
                str(self._cfg(separator_key, " · ")),
            )
        else:
            caption_text = str(raw_caption or label)
            base_label = caption_text
            custom_text = ""
        group.update(
            {
                "label": label,
                "caption": caption_text,
                "completed": len(items) - len(pending_items),
                "pending": len(pending_items),
                "albums": 1 if pending_items else 0,
                "album_plans": [
                    {
                        "key": group.get("album_key", ""),
                        "number": 1,
                        "items": items,
                        "pending_items": pending_items,
                        "caption": {
                            "text": caption_text,
                            "base_label": base_label,
                            "custom_text": custom_text,
                        },
                    }
                ],
            }
        )
        return group

    def set_result(self, result: dict):
        result = dict(result or {})
        completed_paths = set()
        normalized_completed_paths = []
        for path in result.get("completed_paths", []):
            stable = self._stable(path)
            if stable not in completed_paths:
                completed_paths.add(stable)
                normalized_completed_paths.append(stable)
        result["completed_paths"] = normalized_completed_paths
        result["groups"] = [
            self._normalize_legacy_group(group, completed_paths)
            for group in result.get("groups", [])
        ]
        self.result = result
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            for group in result["groups"]:
                label = (
                    f"{group['label']} · {len(group['items'])} 个 · "
                    f"已完成 {group['completed']} · 待上传 {group['pending']} · "
                    f"{group['albums']} 组待上传"
                )
                top = QTreeWidgetItem(["分组", group["label"], "", label])
                if self.kind in {"video", "mixed"}:
                    self.tree.addTopLevelItem(top)
                    top.setExpanded(True)
                plans = group.get("album_plans") or [{
                    "key": "",
                    "number": 1,
                    "items": group["items"],
                    "pending_items": [
                        item for item in group["items"]
                        if self._stable(_item_path(item)) not in completed_paths
                    ],
                    "caption": {
                        "text": group.get("caption", ""),
                        "base_label": group.get("caption", ""),
                        "custom_text": "",
                    },
                }]
                for plan in plans:
                    album_items = plan.get("items", [])
                    pending_count = len(plan.get("pending_items", []))
                    completed_count = len(album_items) - pending_count
                    caption = plan.get("caption", {}) or {}
                    include_key = {
                        "video": "VIDEO_CAPTION_INCLUDE_FILENAMES",
                        "mixed": "MIXED_CAPTION_INCLUDE_FILENAMES",
                        "image": "IMAGE_CAPTION_INCLUDE_FILENAMES",
                    }[self.kind]
                    number_key = {
                        "video": "VIDEO_CAPTION_INCLUDE_FILENAME_NUMBERS",
                        "mixed": "MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS",
                        "image": "IMAGE_CAPTION_INCLUDE_FILENAME_NUMBERS",
                    }[self.kind]
                    caption_text = self.services.filename_description(
                        caption.get("text", ""),
                        album_items,
                        bool(self._cfg(include_key, False)),
                        bool(self._cfg(number_key, True)),
                        max_chars=int(self.services.caption_limit),
                    )
                    album_row = QTreeWidgetItem([
                        "待上传" if pending_count else "已完成",
                        self._album_title(plan),
                        self.services.size_formatter(sum(self._size(item) for item in album_items)),
                        f"{len(album_items)} 个文件 · 已完成 {completed_count} · 待上传 {pending_count}",
                    ])
                    album_row.setData(0, Qt.ItemDataRole.UserRole, plan)
                    album_row.setToolTip(1, caption_text or "无标题")
                    if self.kind == "image":
                        self.tree.addTopLevelItem(album_row)
                    else:
                        top.addChild(album_row)
                    for item in album_items:
                        path = _item_path(item)
                        completed = self._stable(path) in completed_paths
                        date_value = item.get("capture_time") if isinstance(item, Mapping) else None
                        if self.kind == "mixed" and isinstance(item, Mapping):
                            date_value = "图片" if item.get("media_kind") == "image" else "视频"
                        row = QTreeWidgetItem([
                            "待上传" if not completed else "已完成",
                            self._format_date(date_value),
                            self.services.size_formatter(self._size(item)),
                            str(path),
                        ])
                        album_row.addChild(row)
                        row.setToolTip(3, str(path))
                        if isinstance(item, Mapping):
                            source = item.get("date_tag") or "未知"
                            row.setToolTip(
                                1,
                                f"{self._format_date(date_value)}\n日期来源：{source}",
                            )
            for column, width in enumerate((140, 250, 100)):
                self.tree.setColumnWidth(column, width)
            self._filter_preview()
        finally:
            self.tree.setUpdatesEnabled(True)

        if hasattr(self, "chip_files"):
            self.chip_files.setText(f"{result['total_files']} 个")
            self.chip_bytes.setText(self.services.size_formatter(result['total_bytes']))
            self.chip_done.setText(f"{result['completed_files']} 个")
            self.chip_pending.setText(f"{result['pending_files']} 个")
            self.chip_albums.setText(f"{result['album_count']} 组")

        self.summary_label.setText(
            f"共 {result['total_files']} 个 · {self.services.size_formatter(result['total_bytes'])} · "
            f"已完成 {result['completed_files']} · 待上传 {result['pending_files']} · "
            f"{result['album_count']} 组待上传"
        )
        if result.get("missing"):
            self.summary_label.setText(self.summary_label.text() + f" · 缺失日期 {len(result['missing'])}")
        if result.get("scan_skipped_files"):
            self.summary_label.setText(
                self.summary_label.text()
                + f" · 扫描跳过超限 {result['scan_skipped_files']} 个"
            )
        if result.get("scan_compress_files"):
            self.summary_label.setText(
                self.summary_label.text()
                + f" · 上传时压缩 {result['scan_compress_files']} 个"
            )
        if result.get("warning"):
            self.status_label.setText(result["warning"])
        elif result["total_files"] == 0:
            self.status_label.setText("目录中没有支持的媒体文件，请检查目录或文件格式")
        elif result["pending_files"] == 0:
            self.status_label.setText("全部项目已在断点记录中")
        elif not result["core_available"]:
            self.status_label.setText("预览可用；安装完整依赖后才能上传")
        else:
            self.status_label.setText("扫描完成，可开始上传")
        self.start_button.setEnabled(
            bool(
                result["pending_files"]
                and result["core_available"]
                and not result.get("cancelled")
                and not self._running
            )
        )

    @staticmethod
    def _format_date(value) -> str:
        if value is None:
            return "—"
        formatter = getattr(value, "strftime", None)
        if callable(formatter):
            return formatter("%Y-%m-%d %H:%M:%S")
        return str(value)

    def set_cancelled(self, result: dict | None = None):
        self.result = result
        self.tree.clear()
        if hasattr(self, "chip_files"):
            self.chip_files.setText("—")
            self.chip_bytes.setText("—")
            self.chip_done.setText("—")
            self.chip_pending.setText("—")
            self.chip_albums.setText("—")
        self.summary_label.setText("扫描已取消；请重新扫描以获取完整列表")
        self.status_label.setText("扫描已取消")
        self.start_button.setEnabled(False)
        self._update_edit_button()

    def _show_tree_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None or self._running or self._scanning:
            return
        plan = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(plan, dict) and item.parent() is not None:
            item = item.parent()
            plan = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(plan, dict) or not plan.get("key"):
            return
        menu = QMenu(self)
        edit_action = menu.addAction("编辑标题")
        action = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if action == edit_action:
            self._edit_album(item)

    def _edit_album(self, item, _column=0):
        if item is None or self._running or self._scanning:
            return
        plan = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(plan, dict) and item.parent() is not None:
            item = item.parent()
            plan = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(plan, dict) or not plan.get("key"):
            return
        current = plan.get("caption") or {}
        base_label = str(current.get("base_label", ""))
        custom_text = str(current.get("custom_text", ""))
        separator_key = {
            "video": "VIDEO_ALBUM_CAPTION_SEPARATOR",
            "mixed": "MIXED_ALBUM_CAPTION_SEPARATOR",
            "image": "IMAGE_ALBUM_CAPTION_SEPARATOR",
        }[self.kind]
        separator = str(self._cfg(separator_key, " · "))
        include_key = {
            "video": "VIDEO_CAPTION_INCLUDE_FILENAMES",
            "mixed": "MIXED_CAPTION_INCLUDE_FILENAMES",
            "image": "IMAGE_CAPTION_INCLUDE_FILENAMES",
        }[self.kind]
        number_key = {
            "video": "VIDEO_CAPTION_INCLUDE_FILENAME_NUMBERS",
            "mixed": "MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS",
            "image": "IMAGE_CAPTION_INCLUDE_FILENAME_NUMBERS",
        }[self.kind]
        caption_limit = int(self.services.caption_limit)

        dialog = CaptionEditDialog(
            base_label=base_label,
            custom_text=custom_text,
            separator=separator,
            items=plan.get("items", []),
            kind=self.kind,
            caption_limit=caption_limit,
            include_filenames=bool(self._cfg(include_key, False)),
            include_filename_numbers=bool(self._cfg(number_key, True)),
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_base = dialog.base_text
            new_custom = dialog.custom_text
            caption = self.services.compose_caption(new_base, new_custom, separator)
            try:
                rendered = self.services.validate_caption(caption, limit=caption_limit)
            except CaptionLimitError:
                return
            plan["caption"] = {
                "text": rendered,
                "base_label": new_base,
                "custom_text": new_custom,
            }
            item.setData(0, Qt.ItemDataRole.UserRole, plan)
            item.setText(1, self._album_title(plan))
            caption_text = self.services.filename_description(
                rendered,
                plan.get("items", []),
                bool(self._cfg(include_key, False)),
                bool(self._cfg(number_key, True)),
                max_chars=caption_limit,
            )
            item.setToolTip(1, caption_text or "无标题")
            store = self.services.caption_store_factory(self.kind)
            store.set(
                plan["key"],
                base_label=new_base,
                custom_text=new_custom,
            )

    def _update_edit_button(self):
        item = self.tree.currentItem()
        if item is not None and not isinstance(item.data(0, Qt.ItemDataRole.UserRole), dict):
            item = item.parent()
        self.edit_caption_button.setEnabled(
            bool(
                item is not None
                and isinstance(item.data(0, Qt.ItemDataRole.UserRole), dict)
                and not self._running
                and not self._scanning
            )
        )

    def _album_title(self, plan: dict) -> str:
        caption = plan.get("caption", {}) or {}
        text = str(caption.get("base_label") or caption.get("text") or "").strip()
        num = plan.get("number")
        if text and num is not None:
            return f"第 {num} 组 · {text}"
        if num is not None:
            return f"第 {num} 组"
        return text or "未命名"

    def _scan_button_clicked(self):
        if self._scanning:
            self.scan_cancel_requested.emit(self.kind)
        else:
            self.scan_requested.emit(self.kind)

    def _filter_preview(self):
        query = self.search_edit.text().strip().casefold()

        def visit(row, inherited=False):
            matches = (
                inherited
                or not query
                or query in " ".join(row.text(c) for c in range(4)).casefold()
                or query in row.toolTip(1).casefold()
            )
            visible = False
            for index in range(row.childCount()):
                visible = visit(row.child(index), matches) or visible
            if not row.childCount():
                visible = matches and (not self.pending_only.isChecked() or row.text(0) != "已完成")
            row.setHidden(not visible)
            if query and visible:
                row.setExpanded(True)
            return visible

        for index in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(index))

    def clear_scan_result(self):
        self.result = None
        self.tree.clear()
        if hasattr(self, "chip_files"):
            self.chip_files.setText("—")
            self.chip_bytes.setText("—")
            self.chip_done.setText("—")
            self.chip_pending.setText("—")
            self.chip_albums.setText("—")
        self.summary_label.setText("尚未扫描")
        self.status_label.setText("准备扫描")
        self.start_button.setEnabled(False)
        self._update_edit_button()

    def set_running(self, active: bool):
        self._running = active
        self._update_edit_button()
        self.scan_button.setEnabled(not active)
        self.start_button.setEnabled(
            not active
            and bool(
                self.result
                and self.result.get("pending_files")
                and self.result.get("core_available")
            )
        )
        if active:
            self.status_label.setText("任务运行中，请在任务中心查看进度")

    def refresh_theme(self):
        """Re-apply dynamic status styles when theme is toggled."""
        if hasattr(self, "status_pill") and hasattr(self.status_pill, "refresh_theme"):
            self.status_pill.refresh_theme()
        if hasattr(self, "edit_caption_button"):
            self.edit_caption_button.setIcon(get_svg_icon("edit", 14, 14))
        if hasattr(self, "edit_params_button"):
            self.edit_params_button.setIcon(get_svg_icon("sliders", 14, 14))


class VideoPage(UploadPage):
    def __init__(self, *, services: UploadPageServices | None = None, parent=None):
        super().__init__("video", services=services, parent=parent)


class ImagePage(UploadPage):
    def __init__(self, *, services: UploadPageServices | None = None, parent=None):
        super().__init__("image", services=services, parent=parent)


class MixedPage(UploadPage):
    def __init__(self, *, services: UploadPageServices | None = None, parent=None):
        super().__init__("mixed", services=services, parent=parent)


class UploadHubPage(QWidget):
    """Container hosting Video, Image and Mixed upload workbenches under a segmented selector."""

    def __init__(
        self,
        video_page: UploadPage,
        image_page: UploadPage,
        mixed_page: UploadPage,
        parent=None,
    ):
        super().__init__(parent)
        self.video_page = video_page
        self.image_page = image_page
        self.mixed_page = mixed_page
        self.pages = {
            "video": video_page,
            "image": image_page,
            "mixed": mixed_page,
        }

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Top segmented selector
        top_bar = QWidget()
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(28, 16, 28, 0)
        top_layout.setSpacing(12)

        seg_frame = QFrame()
        seg_frame.setObjectName("segmentedFrame")
        seg_layout = QHBoxLayout(seg_frame)
        seg_layout.setContentsMargins(4, 4, 4, 4)
        seg_layout.setSpacing(4)

        from ..icons import get_svg_icon

        self.buttons: dict[str, QPushButton] = {}
        for kind, label, icon_name in (
            ("video", "视频上传", "video"),
            ("image", "图片上传", "image"),
            ("mixed", "混合上传", "mixed"),
        ):
            btn = QPushButton(label)
            btn.setObjectName("segmentedButton")
            btn.setIcon(get_svg_icon(icon_name, size=16))
            btn.setCheckable(True)
            btn.clicked.connect(lambda _, k=kind: self.set_current_kind(k))
            seg_layout.addWidget(btn)
            self.buttons[kind] = btn

        top_layout.addWidget(seg_frame)
        top_layout.addStretch(1)
        layout.addWidget(top_bar)

        from PySide6.QtWidgets import QScrollArea, QStackedWidget

        self.stack = QStackedWidget()
        self.scroll_areas = {}
        for kind, page in (("video", video_page), ("image", image_page), ("mixed", mixed_page)):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            page.setMinimumWidth(0)
            page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            scroll.setWidget(page)
            self.scroll_areas[kind] = scroll
            self.stack.addWidget(scroll)
        layout.addWidget(self.stack, 1)

        self.set_current_kind("video")

    def set_current_kind(self, kind: str):
        if kind not in self.pages:
            return
        idx = {"video": 0, "image": 1, "mixed": 2}[kind]
        self.stack.setCurrentIndex(idx)
        from ..icons import get_svg_icon
        for k, btn in self.buttons.items():
            active = (k == kind)
            btn.setChecked(active)
            btn.setProperty("active", "true" if active else "false")
            color = "#ffffff" if active else THEME.text_secondary
            btn.setIcon(get_svg_icon(k, color=color, size=16))
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def current_kind(self) -> str:
        idx = self.stack.currentIndex()
        return ("video", "image", "mixed")[idx]

    def refresh_theme(self):
        from ..icons import get_svg_icon
        curr_k = self.current_kind()
        for kind, btn in self.buttons.items():
            active = (kind == curr_k)
            color = "#ffffff" if active else THEME.text_secondary
            btn.setIcon(get_svg_icon(kind, color=color, size=16))
        for p in self.pages.values():
            if hasattr(p, "refresh_theme"):
                p.refresh_theme()


__all__ = [
    "ImagePage",
    "MixedPage",
    "UploadHubPage",
    "UploadPage",
    "UploadPageServices",
    "VideoPage",
]
