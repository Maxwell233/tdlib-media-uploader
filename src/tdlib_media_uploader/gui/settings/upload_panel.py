# -*- coding: utf-8 -*-
"""Upload parameters configuration panel for Video, Image, and Mixed media."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..components.telegram_target_editor import TelegramTargetEditor
from ..config_service import get_cfg as _cfg
from ..icons import get_svg_icon
from .base import SettingsPanel


class UploadPanel(SettingsPanel):
    """Panel managing upload targets and media album packaging rules for video/image/mixed."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initial_values = {}

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(14)

        # 1. Top Segmented Switcher: [ 视频 ] [ 图片 ] [ 混合 ]
        segment_card = QWidget()
        segment_layout = QHBoxLayout(segment_card)
        segment_layout.setContentsMargins(0, 0, 0, 0)
        segment_layout.setSpacing(6)

        self.btn_group = QButtonGroup(self)
        self.btn_group.setExclusive(True)

        self.btn_video = QPushButton("视频上传参数")
        self.btn_image = QPushButton("图片上传参数")
        self.btn_mixed = QPushButton("混合上传参数")

        for idx, (btn, icon_name) in enumerate(
            [(self.btn_video, "video"), (self.btn_image, "image"), (self.btn_mixed, "mixed")]
        ):
            btn.setCheckable(True)
            btn.setObjectName("segmentedButton")
            btn.setIcon(get_svg_icon(icon_name, 14, 14))
            self.btn_group.addButton(btn, idx)
            segment_layout.addWidget(btn)

        segment_layout.addStretch(1)
        main_layout.addWidget(segment_card)

        # 2. Stack of 3 media settings sub-panels
        self.stack = QStackedWidget()

        self.video_subpanel = self._build_video_subpanel()
        self.image_subpanel = self._build_image_subpanel()
        self.mixed_subpanel = self._build_mixed_subpanel()

        self.stack.addWidget(self.video_subpanel)
        self.stack.addWidget(self.image_subpanel)
        self.stack.addWidget(self.mixed_subpanel)

        main_layout.addWidget(self.stack, 1)

        self.btn_video.setChecked(True)
        self.btn_group.idClicked.connect(self._on_segment_clicked)

        self.load()

    def _on_segment_clicked(self, idx: int):
        self.stack.setCurrentIndex(idx)

    # ------------------ VIDEO ------------------
    def _build_video_subpanel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # 1. Target
        card1, l1 = self.create_card("Telegram 上传目标")
        self.video_target_editor = TelegramTargetEditor("video", self)
        self.video_target_editor.changed.connect(self._check_dirty)
        l1.addWidget(self.video_target_editor)
        layout.addWidget(card1)

        # 2. Scan & Sort
        card2, l2 = self.create_card("扫描与排序")
        f2 = QFormLayout()
        f2.setSpacing(10)

        self.video_read_dates = QCheckBox("读取视频日期信息（EXIF / 媒体创建日期）")
        f2.addRow("日期读取", self.video_read_dates)

        self.video_missing_date = QComboBox()
        self.video_missing_date.addItem("使用文件修改时间", "mtime")
        self.video_missing_date.addItem("停止并提示缺失日期", "error")
        f2.addRow("缺失日期", self.video_missing_date)

        self.video_media_creation = QCheckBox("读取媒体创建日期")
        f2.addRow("媒体日期", self.video_media_creation)

        self.video_sort = QComboBox()
        self.video_sort.addItem("按修改时间", "mtime")
        self.video_sort.addItem("按文件夹和文件名（自然数字，从小到大）", "name")
        f2.addRow("扫描排序", self.video_sort)

        self.video_read_dates.toggled.connect(self._update_video_date_fields)
        self.video_read_dates.toggled.connect(self._check_dirty)
        self.video_missing_date.currentIndexChanged.connect(self._check_dirty)
        self.video_media_creation.toggled.connect(self._check_dirty)
        self.video_sort.currentIndexChanged.connect(self._check_dirty)

        l2.addLayout(f2)
        layout.addWidget(card2)

        # 3. Album Packaging
        card3, l3 = self.create_card("Album 相册分组与标题")
        f3 = QFormLayout()
        f3.setSpacing(10)

        self.video_group_mode = QComboBox()
        self.video_group_mode.addItem("按日期分组", "date")
        self.video_group_mode.addItem("按扫描顺序固定分组", "fixed")
        f3.addRow("分组方式", self.video_group_mode)

        self.video_album = QSpinBox()
        self.video_album.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.video_album.setRange(1, 10)
        f3.addRow("每组文件数", self.video_album)

        self.video_group_title = QCheckBox("显示组标题（如 Album 1、25-06）")
        f3.addRow("组标题", self.video_group_title)

        self.video_filenames = QCheckBox("标题包含文件名列表")
        f3.addRow("文件名列表", self.video_filenames)

        self.video_filename_numbers = QCheckBox("文件名显示序号（1、2、3…）")
        f3.addRow("文件名格式", self.video_filename_numbers)

        self.video_separator = QLineEdit()
        f3.addRow("标题分隔符", self.video_separator)

        self.video_group_mode.currentIndexChanged.connect(self._check_dirty)
        self.video_album.valueChanged.connect(self._check_dirty)
        self.video_group_title.toggled.connect(self._check_dirty)
        self.video_filenames.toggled.connect(self._check_dirty)
        self.video_filename_numbers.toggled.connect(self._check_dirty)
        self.video_separator.textChanged.connect(self._check_dirty)

        l3.addLayout(f3)
        layout.addWidget(card3)

        # 4. Preprocessing
        card4, l4 = self.create_card("上传前处理")
        f4 = QFormLayout()
        f4.setSpacing(10)
        self.video_thumbnail = QCheckBox("生成视频缩略图")
        f4.addRow("缩略图", self.video_thumbnail)
        self.video_validate_media = QCheckBox("上传前验证全部媒体可读性")
        f4.addRow("预检验证", self.video_validate_media)

        self.video_thumbnail.toggled.connect(self._check_dirty)
        self.video_validate_media.toggled.connect(self._check_dirty)

        l4.addLayout(f4)
        layout.addWidget(card4)

        layout.addStretch(1)
        scroll.setWidget(container)
        return scroll

    def _update_video_date_fields(self):
        enabled = self.video_read_dates.isChecked()
        self.video_missing_date.setEnabled(enabled)
        self.video_media_creation.setEnabled(enabled)
        self.video_sort.setEnabled(enabled)
        self.video_group_mode.setEnabled(enabled)
        if not enabled:
            name_idx = self.video_sort.findData("name")
            fixed_idx = self.video_group_mode.findData("fixed")
            if name_idx >= 0:
                self.video_sort.setCurrentIndex(name_idx)
            if fixed_idx >= 0:
                self.video_group_mode.setCurrentIndex(fixed_idx)

    # ------------------ IMAGE ------------------
    def _build_image_subpanel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # 1. Target
        card1, l1 = self.create_card("Telegram 上传目标")
        self.image_target_editor = TelegramTargetEditor("image", self)
        self.image_target_editor.changed.connect(self._check_dirty)
        l1.addWidget(self.image_target_editor)
        layout.addWidget(card1)

        # 2. Sort & Album
        card2, l2 = self.create_card("排序与 Album 分组")
        f2 = QFormLayout()
        f2.setSpacing(10)

        self.image_sort = QComboBox()
        self.image_sort.addItem("按文件修改时间", "mtime")
        self.image_sort.addItem("按文件夹和文件名（自然数字，从小到大）", "name")
        f2.addRow("排序方式", self.image_sort)

        self.image_album = QSpinBox()
        self.image_album.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.image_album.setRange(1, 10)
        f2.addRow("每组图片数", self.image_album)

        self.image_numbering = QCheckBox("Album 自动编号（如 1/5）")
        f2.addRow("组编号", self.image_numbering)

        self.image_separator = QLineEdit()
        f2.addRow("标题分隔符", self.image_separator)

        self.image_filenames = QCheckBox("Caption 包含文件名列表")
        f2.addRow("文件名列表", self.image_filenames)

        self.image_sort.currentIndexChanged.connect(self._check_dirty)
        self.image_album.valueChanged.connect(self._check_dirty)
        self.image_numbering.toggled.connect(self._check_dirty)
        self.image_separator.textChanged.connect(self._check_dirty)
        self.image_filenames.toggled.connect(self._check_dirty)

        l2.addLayout(f2)
        layout.addWidget(card2)

        # 3. Preprocessing
        card3, l3 = self.create_card("上传前处理")
        f3 = QFormLayout()
        f3.setSpacing(10)

        self.image_compress = QCheckBox("图片超过 10 MiB 时自动使用 FFmpeg 压缩临时副本")
        f3.addRow("超限压缩", self.image_compress)

        self.image_validate_media = QCheckBox("上传前验证全部图片可读性")
        f3.addRow("预检验证", self.image_validate_media)

        self.image_compress.toggled.connect(self._check_dirty)
        self.image_validate_media.toggled.connect(self._check_dirty)

        l3.addLayout(f3)
        layout.addWidget(card3)

        layout.addStretch(1)
        scroll.setWidget(container)
        return scroll

    # ------------------ MIXED ------------------
    def _build_mixed_subpanel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # 1. Target
        card1, l1 = self.create_card("Telegram 上传目标")
        self.mixed_target_editor = TelegramTargetEditor("mixed", self)
        self.mixed_target_editor.changed.connect(self._check_dirty)
        l1.addWidget(self.mixed_target_editor)
        layout.addWidget(card1)

        # 2. Sort & Album
        card2, l2 = self.create_card("排序与 Album 分组")
        f2 = QFormLayout()
        f2.setSpacing(10)

        self.mixed_sort = QComboBox()
        self.mixed_sort.addItem("按文件夹和文件名（自然数字，从小到大）", "name")
        self.mixed_sort.addItem("按文件修改时间（从旧到新）", "mtime")
        f2.addRow("排序方式", self.mixed_sort)

        self.mixed_album = QSpinBox()
        self.mixed_album.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.mixed_album.setRange(1, 10)
        f2.addRow("每组媒体数", self.mixed_album)

        self.mixed_group_title = QCheckBox("显示组标题（文件夹名称）")
        f2.addRow("组标题", self.mixed_group_title)

        self.mixed_filenames = QCheckBox("Caption 包含文件名列表")
        f2.addRow("文件名列表", self.mixed_filenames)

        self.mixed_filename_numbers = QCheckBox("文件名显示序号（1、2、3…）")
        f2.addRow("文件名格式", self.mixed_filename_numbers)

        self.mixed_separator = QLineEdit()
        f2.addRow("标题分隔符", self.mixed_separator)

        self.mixed_sort.currentIndexChanged.connect(self._check_dirty)
        self.mixed_album.valueChanged.connect(self._check_dirty)
        self.mixed_group_title.toggled.connect(self._check_dirty)
        self.mixed_filenames.toggled.connect(self._check_dirty)
        self.mixed_filename_numbers.toggled.connect(self._check_dirty)
        self.mixed_separator.textChanged.connect(self._check_dirty)

        l2.addLayout(f2)
        layout.addWidget(card2)

        # 3. Preprocessing
        card3, l3 = self.create_card("上传前处理")
        f3 = QFormLayout()
        f3.setSpacing(10)

        self.mixed_thumbnail = QCheckBox("视频生成缩略图")
        f3.addRow("缩略图", self.mixed_thumbnail)

        self.mixed_validate_media = QCheckBox("上传前验证全部媒体可读性")
        f3.addRow("预检验证", self.mixed_validate_media)

        self.mixed_thumbnail.toggled.connect(self._check_dirty)
        self.mixed_validate_media.toggled.connect(self._check_dirty)

        l3.addLayout(f3)
        layout.addWidget(card3)

        layout.addStretch(1)
        scroll.setWidget(container)
        return scroll

    # ------------------ LOAD & DIRTY ------------------
    def load(self):
        self.blockSignals(True)
        # 1. Video
        self.video_target_editor.load("video")
        read_dates = bool(_cfg("VIDEO_READ_DATES", True))
        self.video_read_dates.setChecked(read_dates)
        idx = self.video_missing_date.findData(_cfg("VIDEO_MISSING_DATE_POLICY", "mtime"))
        self.video_missing_date.setCurrentIndex(idx if idx >= 0 else 0)
        self.video_media_creation.setChecked(bool(_cfg("VIDEO_READ_MEDIA_CREATION_DATE", True)))
        idx = self.video_sort.findData(_cfg("VIDEO_SORT_MODE", "mtime"))
        self.video_sort.setCurrentIndex(idx if idx >= 0 else 0)

        group_mode = _cfg(
            "VIDEO_GROUP_MODE",
            "fixed" if _cfg("VIDEO_FORCE_TEN_PER_ALBUM", False) else "date",
        )
        idx = self.video_group_mode.findData(str(group_mode).lower())
        self.video_group_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.video_album.setValue(int(_cfg("VIDEO_ALBUM_SIZE", 10)))
        self.video_group_title.setChecked(bool(_cfg("VIDEO_CAPTION_INCLUDE_GROUP_TITLE", True)))
        self.video_filenames.setChecked(bool(_cfg("VIDEO_CAPTION_INCLUDE_FILENAMES", False)))
        self.video_filename_numbers.setChecked(bool(_cfg("VIDEO_CAPTION_INCLUDE_FILENAME_NUMBERS", True)))
        self.video_separator.setText(str(_cfg("VIDEO_ALBUM_CAPTION_SEPARATOR", " · ")))
        self.video_thumbnail.setChecked(bool(_cfg("VIDEO_GENERATE_THUMBNAIL", True)))
        self.video_validate_media.setChecked(bool(_cfg("VIDEO_VALIDATE_MEDIA", False)))
        self._update_video_date_fields()

        # 2. Image
        self.image_target_editor.load("image")
        conf_sort = str(_cfg("IMAGE_SORT_MODE", "mtime")).strip().lower()
        if conf_sort == "path":
            conf_sort = "name"
        idx = self.image_sort.findData(conf_sort)
        self.image_sort.setCurrentIndex(idx if idx >= 0 else 0)
        self.image_album.setValue(int(_cfg("IMAGE_ALBUM_SIZE", 10)))
        self.image_numbering.setChecked(bool(_cfg("IMAGE_ALBUM_NUMBERING", True)))
        self.image_separator.setText(str(_cfg("IMAGE_ALBUM_CAPTION_SEPARATOR", " · ")))
        self.image_filenames.setChecked(bool(_cfg("IMAGE_CAPTION_INCLUDE_FILENAMES", False)))
        self.image_compress.setChecked(bool(_cfg("IMAGE_COMPRESS_OVERSIZE", False)))
        self.image_validate_media.setChecked(bool(_cfg("IMAGE_VALIDATE_MEDIA", False)))

        # 3. Mixed
        self.mixed_target_editor.load("mixed")
        idx = self.mixed_sort.findData(_cfg("MIXED_SORT_MODE", "name"))
        self.mixed_sort.setCurrentIndex(idx if idx >= 0 else 0)
        self.mixed_album.setValue(int(_cfg("MIXED_ALBUM_SIZE", 10)))
        self.mixed_group_title.setChecked(bool(_cfg("MIXED_CAPTION_INCLUDE_GROUP_TITLE", True)))
        self.mixed_filenames.setChecked(bool(_cfg("MIXED_CAPTION_INCLUDE_FILENAMES", False)))
        self.mixed_filename_numbers.setChecked(bool(_cfg("MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS", True)))
        self.mixed_separator.setText(str(_cfg("MIXED_ALBUM_CAPTION_SEPARATOR", " · ")))
        self.mixed_thumbnail.setChecked(bool(_cfg("MIXED_GENERATE_THUMBNAIL", True)))
        self.mixed_validate_media.setChecked(bool(_cfg("MIXED_VALIDATE_MEDIA", False)))

        self.blockSignals(False)
        self._initial_values = self._current_values_dict()
        self.mark_clean()

    def _current_values_dict(self) -> dict:
        return {
            "video_read_dates": self.video_read_dates.isChecked(),
            "video_missing_date": self.video_missing_date.currentData(),
            "video_media_creation": self.video_media_creation.isChecked(),
            "video_sort": self.video_sort.currentData(),
            "video_group_mode": self.video_group_mode.currentData(),
            "video_album": self.video_album.value(),
            "video_group_title": self.video_group_title.isChecked(),
            "video_filenames": self.video_filenames.isChecked(),
            "video_filename_numbers": self.video_filename_numbers.isChecked(),
            "video_separator": self.video_separator.text(),
            "video_thumbnail": self.video_thumbnail.isChecked(),
            "video_validate_media": self.video_validate_media.isChecked(),
            "image_sort": self.image_sort.currentData(),
            "image_album": self.image_album.value(),
            "image_numbering": self.image_numbering.isChecked(),
            "image_separator": self.image_separator.text(),
            "image_filenames": self.image_filenames.isChecked(),
            "image_compress": self.image_compress.isChecked(),
            "image_validate_media": self.image_validate_media.isChecked(),
            "mixed_sort": self.mixed_sort.currentData(),
            "mixed_album": self.mixed_album.value(),
            "mixed_group_title": self.mixed_group_title.isChecked(),
            "mixed_filenames": self.mixed_filenames.isChecked(),
            "mixed_filename_numbers": self.mixed_filename_numbers.isChecked(),
            "mixed_separator": self.mixed_separator.text(),
            "mixed_thumbnail": self.mixed_thumbnail.isChecked(),
            "mixed_validate_media": self.mixed_validate_media.isChecked(),
        }

    def _check_dirty(self):
        targets_dirty = (
            self.video_target_editor.is_dirty()
            or self.image_target_editor.is_dirty()
            or self.mixed_target_editor.is_dirty()
        )
        self.set_dirty(targets_dirty or (self._current_values_dict() != self._initial_values))

    def validate(self) -> tuple[bool, str]:
        for editor, name in (
            (self.video_target_editor, "视频"),
            (self.image_target_editor, "图片"),
            (self.mixed_target_editor, "混合"),
        ):
            ok, err = editor.validate()
            if not ok:
                return False, f"{name}上传目标错误：{err}"
        return True, ""

    def collect_values(self) -> dict:
        values = {}
        values.update(self.video_target_editor.collect_values("video"))
        values.update(self.image_target_editor.collect_values("image"))
        values.update(self.mixed_target_editor.collect_values("mixed"))

        read_dates = self.video_read_dates.isChecked()
        values.update({
            ("video", "read_dates"): read_dates,
            ("video", "missing_date_policy"): self.video_missing_date.currentData(),
            ("video", "read_media_creation_date"): self.video_media_creation.isChecked(),
            ("video", "sort_mode"): self.video_sort.currentData() if read_dates else "name",
            ("video", "group_mode"): self.video_group_mode.currentData() if read_dates else "fixed",
            ("video", "album_size"): self.video_album.value(),
            ("video", "force_ten_per_album"): (
                self.video_group_mode.currentData() == "fixed" if read_dates else True
            ),
            ("video", "caption_include_group_title"): self.video_group_title.isChecked(),
            ("video", "caption_include_filenames"): self.video_filenames.isChecked(),
            ("video", "caption_include_filename_numbers"): self.video_filename_numbers.isChecked(),
            ("video", "album_caption_separator"): self.video_separator.text(),
            ("video", "generate_thumbnail"): self.video_thumbnail.isChecked(),
            ("video", "validate_media"): self.video_validate_media.isChecked(),
        })

        values.update({
            ("image", "sort_mode"): self.image_sort.currentData(),
            ("image", "album_size"): self.image_album.value(),
            ("image", "album_numbering"): self.image_numbering.isChecked(),
            ("image", "album_caption_separator"): self.image_separator.text(),
            ("image", "caption_include_filenames"): self.image_filenames.isChecked(),
            ("image", "compress_oversize"): self.image_compress.isChecked(),
            ("image", "validate_media"): self.image_validate_media.isChecked(),
        })

        values.update({
            ("mixed", "sort_mode"): self.mixed_sort.currentData(),
            ("mixed", "album_size"): self.mixed_album.value(),
            ("mixed", "caption_include_group_title"): self.mixed_group_title.isChecked(),
            ("mixed", "caption_include_filenames"): self.mixed_filenames.isChecked(),
            ("mixed", "caption_include_filename_numbers"): self.mixed_filename_numbers.isChecked(),
            ("mixed", "album_caption_separator"): self.mixed_separator.text(),
            ("mixed", "generate_thumbnail"): self.mixed_thumbnail.isChecked(),
            ("mixed", "validate_media"): self.mixed_validate_media.isChecked(),
        })
        return values

    def mark_clean(self):
        super().mark_clean()
        self.video_target_editor.mark_clean()
        self.image_target_editor.mark_clean()
        self.mixed_target_editor.mark_clean()


__all__ = ["UploadPanel"]
