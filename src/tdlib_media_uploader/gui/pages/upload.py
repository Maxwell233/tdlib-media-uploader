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
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.album import (
    CaptionLimitError,
    CaptionStore,
    compose_caption,
    validate_caption,
    with_filename_description,
)
from ...core.filesystem_legacy import stable_path
from ...config.paths import RESOURCE_DIR
from ..theme import THEME

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


def _format_size(value: float | int | None) -> str:
    value = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


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

    def __init__(self, kind: str, *, services: UploadPageServices | None = None, parent=None):
        super().__init__(parent)
        self.services = services or UploadPageServices()
        self.kind = self.services.require_kind(kind)
        self.result = None
        self._running = False
        self._scanning = False
        accent = self.services.kind_label(self.kind)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        # Title
        title = QLabel(f"{accent}上传工作台")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        # 1. Source directory section
        source_box = QGroupBox("1 · 本地媒体来源目录")
        source_layout = QHBoxLayout(source_box)
        source_layout.setSpacing(10)
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("输入或粘贴媒体目录路径，按 Enter 保存；也可点击右侧浏览选择…")
        self.source_edit.editingFinished.connect(self._commit_source)

        browse = QPushButton("浏览目录…")
        browse.clicked.connect(self._browse)

        source_layout.addWidget(self.source_edit, 1)
        source_layout.addWidget(browse)
        layout.addWidget(source_box)

        # 2. Telegram Target Card
        target_box = QGroupBox(f"2 · Telegram 上传目标（{accent}）")
        target_layout = QGridLayout(target_box)
        target_layout.setSpacing(10)

        target_layout.addWidget(QLabel(f"{accent}目标频道/群组："), 0, 0)
        self.chat_label = QLabel("未配置")
        self.chat_label.setObjectName("valueLabel")
        target_layout.addWidget(self.chat_label, 0, 1)

        target_layout.addWidget(QLabel("Forum Topic 话题："), 1, 0)
        self.topic_label = QLabel("未配置")
        self.topic_label.setObjectName("valueLabel")
        target_layout.addWidget(self.topic_label, 1, 1)

        edit_target = QPushButton(f"配置{accent}目标与参数…")
        edit_target.setObjectName("secondaryButton")
        edit_target.clicked.connect(lambda: self.edit_target_requested.emit(self.kind))
        target_layout.addWidget(edit_target, 0, 2, 2, 1)
        layout.addWidget(target_box)

        # 3. Preview Section
        preview_box = QGroupBox("3 · 文件与 Album 媒体组预览")
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.setSpacing(10)

        # Filter toolbar
        filters = QHBoxLayout()
        filters.setSpacing(10)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索文件名、路径或标题…")
        self.search_edit.setClearButtonEnabled(True)

        self.pending_only = QCheckBox("只看待上传")

        self.edit_caption_button = QPushButton("编辑媒体组标题…")
        self.edit_caption_button.setEnabled(False)
        self.edit_caption_button.clicked.connect(lambda: self._edit_album(self.tree.currentItem()))

        expand_btn = QPushButton("展开")
        expand_btn.setObjectName("ghostButton")
        expand_btn.clicked.connect(self.tree.expandAll if hasattr(self, "tree") else lambda: None)

        collapse_btn = QPushButton("折叠")
        collapse_btn.setObjectName("ghostButton")
        collapse_btn.clicked.connect(self.tree.collapseAll if hasattr(self, "tree") else lambda: None)

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
        self.tree.setMinimumHeight(240)
        self.tree.itemDoubleClicked.connect(self._edit_album)
        self.tree.itemSelectionChanged.connect(self._update_edit_button)

        expand_btn.clicked.disconnect()
        expand_btn.clicked.connect(self.tree.expandAll)
        collapse_btn.clicked.disconnect()
        collapse_btn.clicked.connect(self.tree.collapseAll)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self._filter_preview)
        self.search_edit.textChanged.connect(lambda: self.search_timer.start())
        self.pending_only.toggled.connect(self._filter_preview)
        preview_layout.addWidget(self.tree)

        self.summary_label = QLabel("尚未扫描目录")
        self.summary_label.setObjectName("mutedLabel")
        preview_layout.addWidget(self.summary_label)
        layout.addWidget(preview_box, 1)

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

    def set_result(self, result: dict):
        self.result = result
        self.tree.clear()
        completed_paths = set(result.get("completed_paths", []))
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
        self.summary_label.setText("扫描已取消；请重新扫描以获取完整列表")
        self.status_label.setText("扫描已取消")
        self.start_button.setEnabled(False)
        self._update_edit_button()

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
        dialog = self.services.dialog_class(self)
        dialog.setWindowTitle("编辑媒体组标题")
        dialog.resize(560, 350)
        form = QFormLayout(dialog)
        base_edit = QLineEdit(base_label)
        base_edit.setReadOnly(self.kind == "image")
        custom_edit = QPlainTextEdit(custom_text)
        custom_edit.setPlaceholderText("可输入多行；留空表示不追加")
        preview = QPlainTextEdit()
        preview.setReadOnly(True)
        caption_limit = int(self.services.caption_limit)
        caption_count = QLabel()
        caption_count.setObjectName("mutedLabel")
        caption_hint = QLabel(
            f"编辑器使用 {caption_limit} 字符软上限；连接 Telegram 后会按当前账号的最终限制再次确认。"
        )
        caption_hint.setObjectName("mutedLabel")
        caption_hint.setWordWrap(True)
        include_base = (
            self.kind == "video"
            or self.kind == "mixed" and self._cfg("MIXED_CAPTION_INCLUDE_GROUP_TITLE", True)
            or self.kind == "image" and self._cfg("IMAGE_ALBUM_NUMBERING", True)
        )

        def update_preview():
            caption = self.services.compose_caption(
                base_edit.text() if include_base else "",
                custom_edit.toPlainText(),
                separator,
            )
            try:
                rendered = self.services.filename_description(
                    caption,
                    plan.get("items", []),
                    bool(self._cfg(
                        "VIDEO_CAPTION_INCLUDE_FILENAMES"
                        if self.kind == "video"
                        else "MIXED_CAPTION_INCLUDE_FILENAMES"
                        if self.kind == "mixed"
                        else "IMAGE_CAPTION_INCLUDE_FILENAMES",
                        False,
                    )),
                    bool(self._cfg(
                        "VIDEO_CAPTION_INCLUDE_FILENAME_NUMBERS"
                        if self.kind == "video"
                        else "MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS"
                        if self.kind == "mixed"
                        else "IMAGE_CAPTION_INCLUDE_FILENAME_NUMBERS",
                        True,
                    )),
                    max_chars=caption_limit,
                )
                preview.setPlainText(rendered)
                caption_count.setText(
                    f"用户标题 {len(caption)}/{caption_limit} · 发送预览 {len(rendered)}/{caption_limit}"
                )
                caption_count.setStyleSheet("color: #91a2b5")
            except CaptionLimitError as exc:
                preview.setPlainText(str(exc))
                caption_count.setText(f"超出字数限制：{len(caption)}/{caption_limit}")
                caption_count.setStyleSheet("color: #ff7b72")

        base_edit.textChanged.connect(update_preview)
        custom_edit.textChanged.connect(update_preview)
        form.addRow("基础组标题", base_edit)
        form.addRow("自定义追加", custom_edit)
        form.addRow("发送预览", preview)
        form.addRow("", caption_count)
        form.addRow("", caption_hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        update_preview()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        base_label = base_edit.text().strip()
        custom_text = custom_edit.toPlainText().strip()
        try:
            self.services.validate_caption(
                self.services.compose_caption(
                    base_label if include_base else "",
                    custom_text,
                    separator,
                ),
                caption_limit,
            )
        except CaptionLimitError as exc:
            self.services.message_box_class.warning(self, "标题过长", str(exc))
            return
        store = self.services.caption_store_factory(self.kind)
        if self.kind in {"video", "mixed"} and self._cfg(
            "VIDEO_CAPTION_INCLUDE_GROUP_TITLE"
            if self.kind == "video"
            else "MIXED_CAPTION_INCLUDE_GROUP_TITLE",
            True,
        ) and not base_label:
            self.services.message_box_class.warning(self, "未保存", "请填写基础标题。")
            return
        try:
            store.set(plan["key"], base_label=base_label, custom_text=custom_text)
        except OSError as exc:
            self.services.message_box_class.warning(self, "保存失败", str(exc))
            return
        plan["caption"] = {
            "base_label": base_label,
            "custom_text": custom_text,
            "text": self.services.compose_caption(
                base_label if include_base else "",
                custom_text,
                separator,
            ),
        }
        item.setText(1, self._album_title(plan))
        item.setToolTip(1, preview.toPlainText())
        self._filter_preview()
        self.status_label.setText("标题已保存")

    def _album_title(self, plan):
        text = " ".join(plan.get("caption", {}).get("text", "").split())
        label = f"媒体组 {plan.get('number', 1)}"
        return label if text in {"", str(plan.get("number", 1)), f"Album {plan.get('number', 1)}"} else f"{label} · {text[:100]}"

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


class VideoPage(UploadPage):
    def __init__(self, *, services: UploadPageServices | None = None, parent=None):
        super().__init__("video", services=services, parent=parent)


class ImagePage(UploadPage):
    def __init__(self, *, services: UploadPageServices | None = None, parent=None):
        super().__init__("image", services=services, parent=parent)


class MixedPage(UploadPage):
    def __init__(self, *, services: UploadPageServices | None = None, parent=None):
        super().__init__("mixed", services=services, parent=parent)


__all__ = [
    "ImagePage",
    "MixedPage",
    "UploadPage",
    "UploadPageServices",
    "VideoPage",
]
