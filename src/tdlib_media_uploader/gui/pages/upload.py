# -*- coding: utf-8 -*-
"""Modern package-owned media upload page shared by the three media routes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTreeView,
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
from ...core.filesystem import stable_path
from ...core.identity import canonical_target
from ...config.paths import RESOURCE_DIR
from ...upload.queue import AlbumStatus, TaskStatus
from ...upload.reconciliation import ReconciledAlbum
from ..icons import get_svg_icon
from ..theme import THEME
from ..config_service import get_cfg as _config_getter, target_for as _target_getter
from ..models import item_size as _item_size
from ..preview_tree import PreviewRow, PreviewTreeFilter, PreviewTreeModel
from ..tools import (
    KIND_PATH_KEYS,
    existing_directory_for,
    format_size as _format_size,
    kind_label as _kind_label,
    open_in_file_manager,
    path_text as _path_text,
    require_kind as _require_kind,
)


def _item_path(value: Any) -> Path:
    if isinstance(value, Mapping):
        value = value.get("path")
    return Path(value)


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
    reset_state_requested = Signal(str)
    edit_target_requested = Signal(str)
    edit_parameters_requested = Signal(str)

    def __init__(self, kind: str, *, services: UploadPageServices | None = None, parent=None):
        super().__init__(parent)
        self.services = services or UploadPageServices()
        self.kind = self.services.require_kind(kind)
        self.result = None
        self._running = False
        self._scanning = False
        self._target_label_provider: Callable[[Mapping[str, Any]], Mapping[str, str]] | None = None
        self._file_rows = {}
        self._album_rows = {}
        self._group_rows = []
        self._album_delivery: dict[str, str] = {}
        self._album_errors: dict[str, str] = {}
        self._file_delivery: dict[str, str] = {}
        self._file_errors: dict[str, str] = {}
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
        target_row.setSpacing(8)
        target_lbl = QLabel("Telegram 目标：")
        target_lbl.setObjectName("mutedLabel")
        target_lbl.setFixedWidth(96)

        self.chat_label = QLabel("未配置")
        self.chat_label.setObjectName("valueLabel")

        self.topic_tag = QLabel("Topic:")
        self.topic_tag.setObjectName("mutedLabel")
        self.topic_label = QLabel("未配置")
        self.topic_label.setObjectName("valueLabel")

        edit_target = QPushButton("修改目标")
        edit_target.setObjectName("secondaryButton")
        edit_target.clicked.connect(lambda: self.edit_target_requested.emit(self.kind))

        self.edit_params_button = QPushButton("上传参数")
        self.edit_params_button.setObjectName("secondaryButton")
        self.edit_params_button.setIcon(get_svg_icon("sliders", 14, 14))
        self.edit_params_button.setToolTip(f"跳转至设置页面调整{accent}分卷规则与高级参数")
        self.edit_params_button.clicked.connect(lambda: self.edit_parameters_requested.emit(self.kind))

        target_row.addWidget(target_lbl)
        target_row.addWidget(self.chat_label)
        target_row.addSpacing(10)
        target_row.addWidget(self.topic_tag)
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
        filters.setSpacing(8)

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
        self.edit_caption_button.clicked.connect(lambda: self._edit_album(self.tree.currentIndex()))

        self.toggle_skip_button = QPushButton("跳过该组", self)
        self.toggle_skip_button.setObjectName("secondaryButton")
        self.toggle_skip_button.setToolTip("在加入队列前跳过或恢复当前选中的 Album")
        self.toggle_skip_button.setEnabled(False)
        self.toggle_skip_button.clicked.connect(lambda: self._toggle_album_skip(self.tree.currentIndex()))

        expand_btn = QPushButton("展开全部")
        expand_btn.setObjectName("ghostButton")

        collapse_btn = QPushButton("收起全部")
        collapse_btn.setObjectName("ghostButton")

        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.pending_only)
        filters.addWidget(self.edit_caption_button)
        filters.addWidget(self.toggle_skip_button)
        filters.addWidget(expand_btn)
        filters.addWidget(collapse_btn)
        preview_layout.addLayout(filters)

        self.tree = QTreeView()
        self._preview_model = PreviewTreeModel(self.tree)
        self._preview_filter = PreviewTreeFilter(self._preview_model, self.tree)
        self.tree.setModel(self._preview_filter)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setStretchLastSection(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setMinimumHeight(150)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
        self.tree.doubleClicked.connect(self._edit_album)
        self.tree.selectionModel().currentChanged.connect(self._update_edit_button)

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
        bottom.setSpacing(10)

        self.reset_state_button = QPushButton("重置断点")
        self.reset_state_button.setObjectName("dangerGhostButton")
        self.reset_state_button.setToolTip(f"清空{accent}类型的全部本地断点（操作前会弹窗二次确认）")
        self.reset_state_button.clicked.connect(lambda: self.reset_state_requested.emit(self.kind))

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("valueLabel")

        self.scan_button = QPushButton("扫描目录")
        self.scan_button.setObjectName("secondaryButton")
        self.scan_button.clicked.connect(self._scan_button_clicked)

        self.start_button = QPushButton("加入上传队列")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(lambda: self.start_requested.emit(self.kind))

        bottom.addWidget(self.reset_state_button)
        bottom.addWidget(self.status_label, 1)
        bottom.addWidget(self.scan_button)
        bottom.addWidget(self.start_button)
        layout.addLayout(bottom)

        self.refresh_config()

    def set_target_label_provider(
        self,
        provider: Callable[[Mapping[str, Any]], Mapping[str, str]] | None,
    ):
        self._target_label_provider = provider
        self.refresh_target_label()

    def refresh_target_label(self):
        target = dict(self.services.target_getter(self.kind) or {})
        mode = str(target.get("target_mode", "forum_topic"))
        target_name = "频道" if mode == "channel" else "超级群组"
        chat_id = str(target.get("chat_id", "未配置"))
        topic_id = str(target.get("forum_topic_id", "未配置"))
        cached = dict(self._target_label_provider(target) or {}) if self._target_label_provider else {}
        chat_title = str(cached.get("chat_title", "") or "").strip()
        topic_name = str(cached.get("topic_name", "") or "").strip()

        if chat_title:
            self.chat_label.setText(f"{target_name} · {chat_title}")
            self.chat_label.setToolTip(f"{chat_title}\nChat ID: {chat_id}")
        else:
            self.chat_label.setText(f"{target_name} · {chat_id}")
            self.chat_label.setToolTip(f"Chat ID: {chat_id}")

        is_channel = mode == "channel"
        self.topic_tag.setVisible(not is_channel)
        if is_channel:
            self.topic_label.setText("不使用 Topic")
            self.topic_label.setToolTip("频道目标不使用 Forum Topic")
        elif topic_name:
            self.topic_label.setText(topic_name)
            self.topic_label.setToolTip(f"{topic_name}\nTopic ID: {topic_id}")
        else:
            self.topic_label.setText(topic_id)
            self.topic_label.setToolTip(f"Topic ID: {topic_id}")

    def _cfg(self, name: str, default=None):
        return self.services.config_getter(name, default)

    def _size(self, value: Any) -> int:
        return self.services.item_size(value)

    def _stable(self, value: Any) -> str:
        return str(self.services.stable_path(value))

    def refresh_config(self):
        path_name = self.services.path_keys[self.kind]
        self.source_edit.setText(self.services.path_text(self._cfg(path_name, "")))
        self.refresh_target_label()

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

    def set_scanning(self, active: bool):
        self._scanning = active
        self.scan_button.setText("停止扫描" if active else "扫描目录")
        self.reset_state_button.setEnabled(not active and not self._running)
        if active:
            self.clear_scan_result()
            self.status_label.setText("正在扫描目录…")
        self._update_edit_button()

    @staticmethod
    def _combined_status(delivery_status: str, excluded: bool) -> str:
        if excluded and delivery_status not in {"已完成", "已跳过"}:
            return f"{delivery_status} · 已排除"
        return delivery_status

    def _unfinished_items(self, plan: Mapping[str, Any], completed: set[str]) -> list[Any]:
        return [
            entry for entry in plan.get("pending_items", ())
            if self._stable(_item_path(entry)) not in completed
        ]

    @staticmethod
    def _item_snapshot(item: Any) -> tuple[int, int] | None:
        if isinstance(item, Mapping):
            size_val = item.get("scan_size", item.get("size"))
            mtime_val = item.get("scan_mtime_ns", item.get("mtime_ns"))
        else:
            snapshot = getattr(item, "snapshot", None)
            size_val = getattr(snapshot, "size", None)
            mtime_val = getattr(snapshot, "mtime_ns", None)
        if size_val is None or mtime_val is None:
            return None
        try:
            return int(size_val), int(mtime_val)
        except (TypeError, ValueError):
            return None

    def _update_scan_summary_chips_and_labels(self, result: dict):
        total_files = int(result.get("total_files", 0) or 0)
        total_bytes = int(result.get("total_bytes", 0) or 0)
        completed_files = int(result.get("completed_files", 0) or 0)
        pending_files = int(result.get("pending_files", 0) or 0)
        pending_bytes = int(result.get("pending_bytes", 0) or 0)
        skipped_files = int(result.get("skipped_files", 0) or 0)
        skipped_albums = int(result.get("skipped_albums", 0) or 0)
        album_count = int(result.get("album_count", 0) or 0)

        self.chip_files.setText(f"{total_files} 个")
        self.chip_bytes.setText(self.services.size_formatter(total_bytes))
        self.chip_done.setText(f"{completed_files} 个")
        self.chip_pending.setText(
            f"{pending_files} 个（跳过 {skipped_files}）" if skipped_files else f"{pending_files} 个"
        )
        self.chip_albums.setText(
            f"{album_count} 组（跳过 {skipped_albums}）" if skipped_albums else f"{album_count} 组"
        )

        summary_text = (
            f"共 {total_files} 个 · {self.services.size_formatter(total_bytes)} · "
            f"已完成 {completed_files} · 待上传 {pending_files}"
            f"（{self.services.size_formatter(pending_bytes)}） · "
            f"{album_count} 组待上传"
        )
        if skipped_albums or skipped_files:
            summary_text += f" · 已跳过 {skipped_albums} 组（{skipped_files} 个文件）"
        if result.get("missing"):
            summary_text += f" · 缺失日期 {len(result['missing'])}"
        if result.get("scan_skipped_files"):
            summary_text += f" · 扫描跳过超限 {result['scan_skipped_files']} 个"
        if result.get("scan_compress_files"):
            summary_text += f" · 上传时压缩 {result['scan_compress_files']} 个"
        self.summary_label.setText(summary_text)

        unknown_albums = sum(1 for status in self._album_delivery.values() if status == "待核对")
        if total_files == 0:
            self.status_label.setText("未找到可上传媒体，请检查目录或文件格式")
        elif unknown_albums:
            extra = f"（已排除 {skipped_albums} 组）" if skipped_albums else ""
            self.status_label.setText(
                f"存在待核对记录（{unknown_albums} 组{extra}），请先在“未确认上传”核对保护记录"
            )
        elif result.get("warning"):
            self.status_label.setText(result["warning"])
        elif pending_files == 0 and skipped_files == 0:
            self.status_label.setText("全部项目已上传")
        elif pending_files == 0 and skipped_files > 0:
            self.status_label.setText("所有待上传 Album 已跳过")
        elif skipped_albums > 0:
            self.status_label.setText(
                f"待上传 {pending_files} 个（{album_count} 组），已跳过 {skipped_files} 个（{skipped_albums} 组）"
            )
        else:
            self.status_label.setText("扫描完成，可加入上传队列")
        self.start_button.setEnabled(
            bool(
                pending_files
                and not result.get("cancelled")
            )
        )

    @staticmethod
    def _display_path(path: Path, source_root: Path | None) -> str:
        if source_root is not None:
            try:
                return path.relative_to(source_root).as_posix()
            except ValueError:
                pass
        return str(path)

    def _build_scan_result_tree(self, result: dict, completed_paths: set[str]):
        root = PreviewRow()
        self._file_rows.clear()
        self._album_rows.clear()
        self._group_rows.clear()
        self._album_delivery.clear()
        self._album_errors.clear()
        self._file_delivery.clear()
        self._file_errors.clear()
        skipped = set(result.get("skipped_album_keys", ()))
        source_root = Path(result["source_dir"]) if result.get("source_dir") else None
        for group in result["groups"]:
            label = (
                f"{group['label']} · {len(group['items'])} 个 · "
                f"已完成 {group['completed']} · 待上传 {group['pending']} · "
                f"{group['albums']} 组待上传"
            )
            top = root if self.kind == "image" else PreviewRow(["分组", group["label"], "", label], root)
            self._group_rows.append(top if self.kind != "image" else None)
            for plan in group["album_plans"]:
                album_items = plan.get("items", [])
                unfinished = self._unfinished_items(plan, completed_paths)
                album_excluded = plan["key"] in skipped and bool(unfinished)
                pending_count = 0 if album_excluded else len(unfinished)
                completed_count = sum(
                    self._stable(_item_path(item)) in completed_paths for item in album_items
                )
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
                album_delivery = (
                    "已完成"
                    if not unfinished
                    else ("部分完成" if completed_count else "待上传")
                )
                self._album_delivery[plan["key"]] = album_delivery
                album_status = self._combined_status(album_delivery, album_excluded)
                album_row = PreviewRow(
                    [
                        album_status,
                        self._album_title(plan),
                        self.services.size_formatter(
                            sum(self._size(item) for item in album_items)
                        ),
                        f"{len(album_items)} 个文件 · 已完成 {completed_count} · 待上传 {pending_count}",
                    ],
                    top,
                    plan=plan,
                    delivery_status=album_delivery,
                    excluded=album_excluded,
                )
                self._album_rows[plan["key"]] = album_row
                album_row.tooltips[1] = caption_text or "无标题"
                for item in album_items:
                    path = _item_path(item)
                    stable = self._stable(path)
                    completed = stable in completed_paths
                    date_value = (
                        item.get("capture_time") if isinstance(item, Mapping) else None
                    )
                    if self.kind == "mixed" and isinstance(item, Mapping):
                        date_value = (
                            "图片" if item.get("media_kind") == "image" else "视频"
                        )
                    leaf_delivery = "已完成" if completed else "待上传"
                    self._file_delivery[stable] = leaf_delivery
                    leaf_excluded = not completed and album_excluded
                    leaf_status = self._combined_status(leaf_delivery, leaf_excluded)
                    row = PreviewRow(
                        [
                            leaf_status,
                            self._format_date(date_value),
                            self.services.size_formatter(self._size(item)),
                            self._display_path(path, source_root),
                        ],
                        album_row,
                        delivery_status=leaf_delivery,
                        excluded=leaf_excluded,
                    )
                    self._file_rows[stable] = row
                    row.tooltips[3] = str(path)
                    if isinstance(item, Mapping):
                        source = item.get("date_tag") or "未知"
                        row.tooltips[1] = f"{self._format_date(date_value)}\n日期来源：{source}"
        self._preview_model.set_root(root)
        for column, width in enumerate((148, 240, 96)):
            self.tree.setColumnWidth(column, width)
        if self.kind != "image":
            self.tree.expandToDepth(0)
        self._filter_preview()

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
        self.result = result
        self.tree.setUpdatesEnabled(False)
        try:
            self._build_scan_result_tree(result, completed_paths)
            self._recompute_preview_totals(
                completed_paths, set(result.get("skipped_album_keys", ())), {},
            )
        finally:
            self.tree.setUpdatesEnabled(True)

        self._update_scan_summary_chips_and_labels(result)
        self._update_edit_button()

    def _matches_preview(self, source_root, target):
        result = self.result
        return bool(
            result
            and not result.get("cancelled")
            and target
            and result.get("target")
            and source_root
            and result.get("source_dir")
            and self._stable(result["source_dir"]) == self._stable(source_root)
            and canonical_target(dict(result["target"])) == canonical_target(dict(target))
        )

    def _derive_album_delivery(self, plan: Mapping[str, Any], completed: set[str]) -> tuple[str, str | None]:
        items = plan.get("items", ())
        if not items:
            return "已完成", None
        if all(self._stable(_item_path(item)) in completed for item in items):
            return "已完成", None
        leaf_states = [self._file_delivery.get(self._stable(_item_path(item)), "待上传") for item in items]
        leaf_errors = [
            self._file_errors.get(self._stable(_item_path(item)))
            for item in items
            if self._file_errors.get(self._stable(_item_path(item)))
        ]
        error = leaf_errors[0] if leaf_errors else None
        if "待核对" in leaf_states:
            return "待核对", error
        if "已跳过" in leaf_states and all(state in {"已完成", "已跳过"} for state in leaf_states):
            return "已跳过", error
        if "失败" in leaf_states:
            return "失败", error
        if "暂缓" in leaf_states:
            return "暂缓", error
        if any(self._stable(_item_path(item)) in completed for item in items):
            return "部分完成", None
        return "待上传", None

    def sync_reconciled_album(self, confirmation) -> tuple[bool, str]:
        if self.result is None or self.result.get("cancelled"):
            if isinstance(confirmation, ReconciledAlbum) and not confirmation.syncable:
                return False, confirmation.reason or "旧记录缺少目标或文件快照，已完成持久化对账，请重新扫描目录。"
            return False, ""
        if not isinstance(confirmation, ReconciledAlbum):
            return False, "对账结果格式无效，请重新扫描目录。"
        if not confirmation.syncable:
            return False, confirmation.reason or "旧记录缺少目标、来源目录或完整文件快照，已完成持久化对账，请重新扫描目录。"
        conf_kind = str(confirmation.kind or "").strip().lower()
        if not conf_kind or conf_kind != self.kind:
            return False, ""
        if not confirmation.target or not self.result.get("target"):
            return False, "对账记录缺少 Telegram 目标，已完成持久化对账，请重新扫描目录。"
        if canonical_target(dict(confirmation.target)) != canonical_target(dict(self.result["target"])):
            return False, ""
        if not confirmation.source_root or not self.result.get("source_dir"):
            return False, "对账记录缺少来源目录，已完成持久化对账，请重新扫描目录。"
        if self._stable(confirmation.source_root) != self._stable(self.result["source_dir"]):
            return False, ""

        confirmed_snaps = tuple(
            confirmation.confirmed_snapshots or (confirmation.snapshots if confirmation.sent else ())
        )
        reupload_snaps = tuple(confirmation.reupload_snapshots)
        all_snaps = (*confirmed_snaps, *reupload_snaps)
        if not all_snaps:
            return False, "对账记录缺少完整文件快照，已完成持久化对账，请重新扫描目录。"

        preview_items: dict[str, Any] = {}
        for group in self.result.get("groups", ()):
            for plan in group.get("album_plans", ()):
                for item in plan.get("items", ()):
                    preview_items[self._stable(_item_path(item))] = item
        for item in self.result.get("items", ()):
            preview_items.setdefault(self._stable(_item_path(item)), item)

        for snapshot in all_snaps:
            stable = self._stable(snapshot.path)
            preview_item = preview_items.get(stable)
            if preview_item is None or stable not in self._file_rows:
                return False, "对账文件与当前预览列表不匹配，已完成持久化对账，请重新扫描目录。"
            preview_snap = self._item_snapshot(preview_item)
            if preview_snap is None or preview_snap != (int(snapshot.size), int(snapshot.mtime_ns)):
                return False, "预览中的文件快照与对账记录不一致，已完成持久化对账，请重新扫描目录。"
            try:
                disk_stat = Path(snapshot.path).stat()
            except OSError:
                disk_stat = None
            if disk_stat is not None and (disk_stat.st_size, disk_stat.st_mtime_ns) != (
                int(snapshot.size),
                int(snapshot.mtime_ns),
            ):
                return False, "文件大小或修改时间已发生变化，已完成持久化对账，请重新扫描目录。"

        completed = set(self.result.get("completed_paths", ()))
        skipped = set(self.result.get("skipped_album_keys", ()))
        touched_paths: set[str] = set()
        for snapshot in confirmed_snaps:
            stable = self._stable(snapshot.path)
            completed.add(stable)
            self._file_delivery[stable] = "已完成"
            self._file_errors.pop(stable, None)
            touched_paths.add(stable)
        for snapshot in reupload_snaps:
            stable = self._stable(snapshot.path)
            if stable in completed:
                self._file_delivery[stable] = "已完成"
            else:
                self._file_delivery[stable] = "待上传"
                self._file_errors.pop(stable, None)
            touched_paths.add(stable)

        for group in self.result.get("groups", ()):
            for plan in group.get("album_plans", ()):
                plan_paths = {self._stable(_item_path(item)) for item in plan.get("items", ())}
                if plan_paths & touched_paths:
                    delivery, error = self._derive_album_delivery(plan, completed)
                    self._album_delivery[plan["key"]] = delivery
                    if error:
                        self._album_errors[plan["key"]] = error
                    else:
                        self._album_errors.pop(plan["key"], None)

        self.tree.setUpdatesEnabled(False)
        try:
            self._recompute_preview_totals(completed, skipped, {})
        finally:
            self.tree.setUpdatesEnabled(True)
        self._update_scan_summary_chips_and_labels(self.result)
        self._update_edit_button()
        return True, ""

    def sync_task_outcomes(self, task):
        if not self._matches_preview(task.source_root, task.target):
            return
        completed = set(self.result.get("completed_paths", ()))
        skipped = set(self.result.get("skipped_album_keys", ()))
        outcomes = task.album_statuses()
        known_confirmed = task.known_confirmed
        labels = {
            AlbumStatus.FAILED: "失败",
            AlbumStatus.DEFERRED: "暂缓",
            AlbumStatus.PARTIAL: "部分完成",
            AlbumStatus.UNKNOWN: "待核对",
            AlbumStatus.SKIPPED: "已跳过",
            AlbumStatus.COMPLETED: "已完成",
        }
        task_skipped = task.status is TaskStatus.SKIPPED
        preview_items = {
            self._stable(_item_path(item)): item
            for group in self.result.get("groups", ())
            for plan in group.get("album_plans", ())
            for item in plan.get("items", ())
        }
        for plan in task.plans:
            outcome = outcomes[plan.key]
            failed = set(outcome.failed_paths)
            deferred = set(outcome.deferred_paths)
            all_matched = True
            matched_any = False
            for item in plan.items:
                path = self._stable(item.path)
                preview_item = preview_items.get(path)
                if preview_item is None or self._item_snapshot(preview_item) != item.snapshot.as_tuple():
                    all_matched = False
                    continue
                matched_any = True
                if path in known_confirmed and outcome.status is not AlbumStatus.UNKNOWN:
                    status = "已完成"
                    completed.add(path)
                elif outcome.status is AlbumStatus.UNKNOWN:
                    status = "已完成" if path in completed else "待核对"
                elif path in known_confirmed:
                    status = "已完成"
                    completed.add(path)
                elif outcome.status is AlbumStatus.SKIPPED or task_skipped:
                    status = "已跳过"
                elif path in failed:
                    status = "失败"
                elif path in deferred:
                    status = "暂缓"
                else:
                    continue
                self._file_delivery[path] = status
                if outcome.error and status not in {"已完成"}:
                    self._file_errors[path] = outcome.error
                else:
                    self._file_errors.pop(path, None)
            if plan.key in self._album_rows and matched_any and all_matched:
                if outcome.status in labels:
                    self._album_delivery[plan.key] = labels[outcome.status]
                if outcome.error and outcome.status is not AlbumStatus.COMPLETED:
                    self._album_errors[plan.key] = outcome.error
                else:
                    self._album_errors.pop(plan.key, None)
                if outcome.status is AlbumStatus.SKIPPED or task_skipped:
                    skipped.add(plan.key)

        self.tree.setUpdatesEnabled(False)
        try:
            self._recompute_preview_totals(completed, skipped, {})
        finally:
            self.tree.setUpdatesEnabled(True)
        self._update_scan_summary_chips_and_labels(self.result)
        self._update_edit_button()

    def _recompute_preview_totals(
        self,
        completed: set[str],
        skipped: set[str],
        outcomes: Mapping[str, Any] = (),
        *,
        snapshots: Mapping[str, Any] | None = None,
        task_skipped: bool = False,
        updated_album_keys: set[str] | None = None,
    ):
        del outcomes, snapshots, task_skipped, updated_album_keys
        result = self.result
        completed_files = pending_files = pending_bytes = skipped_files = skipped_albums = album_count = 0
        for group, group_row in zip(result["groups"], self._group_rows):
            group_pending = group_completed = group_albums = group_skipped_files = 0
            for plan in group["album_plans"]:
                key = plan["key"]
                row = self._album_rows[key]
                items = plan["items"]
                pending = self._unfinished_items(plan, completed)
                plan["pending_items"] = pending
                done = sum(self._stable(_item_path(item)) in completed for item in items)
                if not pending:
                    skipped.discard(key)
                excluded = key in skipped and bool(pending)

                for item in items:
                    path = self._stable(_item_path(item))
                    is_done = path in completed
                    if is_done:
                        leaf_delivery = "已完成"
                        leaf_error = "已完成"
                        self._file_delivery[path] = leaf_delivery
                        self._file_errors.pop(path, None)
                    else:
                        leaf_delivery = self._file_delivery.get(path, "待上传")
                        leaf_error = self._file_errors.get(path)
                    leaf_excluded = not is_done and excluded
                    display_leaf = self._combined_status(leaf_delivery, leaf_excluded)
                    leaf_tooltip = (
                        f"{leaf_error}（本次已排除入队）"
                        if leaf_excluded and leaf_error
                        else (leaf_error or display_leaf)
                    )
                    self._preview_model.update_row(
                        self._file_rows[path],
                        text={0: display_leaf},
                        tooltips={0: leaf_tooltip},
                        delivery_status=leaf_delivery,
                        excluded=leaf_excluded,
                    )

                if not pending:
                    album_delivery = "已完成"
                    self._album_delivery[key] = album_delivery
                    self._album_errors.pop(key, None)
                else:
                    album_delivery = self._album_delivery.get(key)
                    if not album_delivery or album_delivery == "已完成":
                        album_delivery, derived_err = self._derive_album_delivery(plan, completed)
                        self._album_delivery[key] = album_delivery
                        if derived_err:
                            self._album_errors[key] = derived_err

                display_album = self._combined_status(album_delivery, excluded)
                album_error = self._album_errors.get(key)
                album_tooltip = (
                    f"{album_error}（本次已排除入队）"
                    if excluded and album_error
                    else (album_error or display_album)
                )

                selected_count = 0 if excluded else len(pending)
                excluded_count = len(pending) if excluded else 0
                desc = f"{len(items)} 个文件 · 已完成 {done} · 待上传 {selected_count}"
                if excluded_count:
                    desc += f" · 已跳过 {excluded_count}"

                self._preview_model.update_row(
                    row,
                    text={0: display_album, 3: desc},
                    tooltips={0: album_tooltip},
                    delivery_status=album_delivery,
                    excluded=excluded,
                )

                completed_files += done
                group_completed += done
                if excluded:
                    skipped_albums += 1
                    skipped_files += len(pending)
                    group_skipped_files += len(pending)
                elif pending:
                    album_count += 1
                    pending_files += len(pending)
                    pending_bytes += sum(self._size(item) for item in pending)
                    group_pending += len(pending)
                    group_albums += 1
            if not group["album_plans"]:
                group_pending = group["pending"]
            group.update(pending=group_pending, completed=group_completed, albums=group_albums)
            if group_row is not None:
                label = group["label"]
                group_desc = (
                    f"{label} · {len(group['items'])} 个 · 已完成 {group_completed} · "
                    f"待上传 {group_pending} · {group_albums} 组待上传"
                )
                if group_skipped_files:
                    group_desc += f" · 已跳过 {group_skipped_files}"
                self._preview_model.update_row(group_row, text={3: group_desc})
        if not result.get("groups"):
            completed_files = len(completed)
        result["plans"] = tuple(
            replace(plan, pending_items=tuple(
                item for item in plan.pending_items if self._stable(item.path) not in completed
            ))
            for plan in result["plans"]
        )
        result.update(
            completed_paths=sorted(completed),
            completed_files=completed_files,
            pending_files=pending_files,
            pending_bytes=pending_bytes,
            skipped_files=skipped_files,
            skipped_albums=skipped_albums,
            album_count=album_count,
            skipped_album_keys=tuple(sorted(skipped)),
        )
        self._filter_preview()

    def _toggle_album_skip(self, index):
        item = self._album_row(index)
        if item is None or self._scanning or self.result is None:
            return
        plan = item.plan
        key = plan["key"]
        completed = set(self.result.get("completed_paths", ()))
        skipped = set(self.result.get("skipped_album_keys", ()))
        pending = self._unfinished_items(plan, completed)
        if not pending and key not in skipped:
            return
        if key in skipped:
            skipped.remove(key)
        else:
            skipped.add(key)
        self.tree.setUpdatesEnabled(False)
        try:
            self._recompute_preview_totals(completed, skipped, {})
        finally:
            self.tree.setUpdatesEnabled(True)
        self._update_scan_summary_chips_and_labels(self.result)
        self._update_edit_button()

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
        self._clear_preview_rows()
        self.chip_files.setText("—")
        self.chip_bytes.setText("—")
        self.chip_done.setText("—")
        self.chip_pending.setText("—")
        self.chip_albums.setText("—")
        self.summary_label.setText("扫描已取消；请重新扫描以获取完整列表")
        self.status_label.setText("扫描已取消")
        self.start_button.setEnabled(False)
        self._update_edit_button()

    def _selected_leaf_path(self, index) -> str:
        if not index.isValid():
            return ""
        row = self._preview_model.row(self._preview_filter.mapToSource(index))
        if row.plan is not None or not row.parent or row.parent.plan is None:
            return ""
        return str(row.tooltips.get(3) or "")

    def _show_tree_context_menu(self, pos):
        index = self.tree.indexAt(pos)
        album_item = self._album_row(index)
        if self._scanning or album_item is None:
            return
        index = QPersistentModelIndex(index)
        menu = QMenu(self)
        edit_action = menu.addAction("编辑标题")
        key = album_item.plan["key"]
        skipped = set((self.result or {}).get("skipped_album_keys", ()))
        completed = set((self.result or {}).get("completed_paths", ()))
        has_pending = bool(self._unfinished_items(album_item.plan, completed))
        skip_action = None
        if has_pending or key in skipped:
            skip_action = menu.addAction("恢复此 Album（纳入本次入队）" if key in skipped else "跳过此 Album（暂不入队）")
        leaf_path = self._selected_leaf_path(QModelIndex(index))
        open_action = copy_action = None
        if leaf_path:
            menu.addSeparator()
            open_action = menu.addAction("打开所在目录")
            open_action.setEnabled(existing_directory_for(leaf_path) is not None)
            copy_action = menu.addAction("复制文件路径")
        action = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if action == edit_action:
            self._edit_album(QModelIndex(index))
        elif skip_action is not None and action == skip_action:
            self._toggle_album_skip(QModelIndex(index))
        elif open_action is not None and action == open_action:
            open_in_file_manager(leaf_path)
        elif copy_action is not None and action == copy_action:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(leaf_path)

    def _album_row(self, index):
        if not index.isValid():
            return None
        row = self._preview_model.row(self._preview_filter.mapToSource(index))
        album = row if row.plan is not None else row.parent
        return album if album is not None and album.plan is not None else None

    def _edit_album(self, index):
        item = self._album_row(index)
        if item is None or self._scanning:
            return
        plan = item.plan
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
            except CaptionLimitError as exc:
                self.status_label.setText(f"标题未保存：{exc}")
                QMessageBox.warning(self, "标题过长", str(exc))
                return
            try:
                store = self.services.caption_store_factory(self.kind)
                store.set(plan["key"], base_label=new_base, custom_text=new_custom)
            except (OSError, RuntimeError, ValueError) as exc:
                QMessageBox.warning(self, "标题保存失败", str(exc))
                return
            if self.result is not None:
                self.result.setdefault("caption_overrides", {})[plan["key"]] = rendered
            plan["caption"] = {
                "text": rendered,
                "base_label": new_base,
                "custom_text": new_custom,
            }
            caption_text = self.services.filename_description(
                rendered,
                plan.get("items", []),
                bool(self._cfg(include_key, False)),
                bool(self._cfg(number_key, True)),
                max_chars=caption_limit,
            )
            self._preview_model.update_row(item, text={1: self._album_title(plan)},
                                           tooltips={1: caption_text or "无标题"})
            self._filter_preview()


    def _update_edit_button(self):
        album_item = self._album_row(self.tree.currentIndex())
        enabled = not self._scanning and album_item is not None
        self.edit_caption_button.setEnabled(enabled)
        if not enabled or self.result is None:
            self.toggle_skip_button.setEnabled(False)
            self.toggle_skip_button.setText("跳过该组")
            return
        key = album_item.plan["key"]
        skipped = set(self.result.get("skipped_album_keys", ()))
        completed = set(self.result.get("completed_paths", ()))
        has_pending = bool(self._unfinished_items(album_item.plan, completed))
        is_skipped = key in skipped
        self.toggle_skip_button.setText("恢复该组" if is_skipped else "跳过该组")
        self.toggle_skip_button.setEnabled(has_pending or is_skipped)

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
        self._preview_filter.set_filters(query, self.pending_only.isChecked())
        if query:
            self.tree.expandAll()

    def _clear_preview_rows(self):
        self._preview_model.set_root()
        self._file_rows.clear()
        self._album_rows.clear()
        self._group_rows.clear()
        self._album_delivery.clear()
        self._album_errors.clear()
        self._file_delivery.clear()
        self._file_errors.clear()
        self._filter_preview()

    def clear_scan_result(self):
        self.result = None
        self._clear_preview_rows()
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
        self.reset_state_button.setEnabled(not active and not self._scanning)

    def set_queue_immediate(self, immediate: bool):
        self.start_button.setText("立即上传" if immediate else "加入上传队列")

    def refresh_theme(self):
        """Re-apply dynamic status styles when theme is toggled."""
        self.edit_caption_button.setIcon(get_svg_icon("edit", 14, 14))
        self.edit_params_button.setIcon(get_svg_icon("sliders", 14, 14))
        self._preview_model.layoutChanged.emit()


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
            p.refresh_theme()


__all__ = [
    "ImagePage",
    "MixedPage",
    "UploadHubPage",
    "UploadPage",
    "UploadPageServices",
    "VideoPage",
]
