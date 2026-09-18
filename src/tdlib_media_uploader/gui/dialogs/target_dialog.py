# -*- coding: utf-8 -*-
"""Target and media-route configuration dialog for Beta 3."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
)

from ...config.paths import read_version
from ..theme import THEME


def _cfg(name: str, default=None):
    from ..main_window import _cfg as get_cfg

    return get_cfg(name, default)


def _target_for(kind: str) -> dict:
    from ..main_window import _target_for as get_target

    return get_target(kind)


def _require_kind(kind: str) -> str:
    from ..main_window import _require_kind as req_kind

    return req_kind(kind)


def _kind_label(kind: str) -> str:
    from ..main_window import _kind_label as get_label

    return get_label(kind)


def _write_config_values(values: dict[tuple[str, str], object]) -> str:
    from ..main_window import _write_config_values as write_vals

    return write_vals(values)


class TargetDialog(QDialog):
    """Edit the Telegram target and media options used by the selected media uploader."""

    def __init__(self, kind="video", parent=None):
        if not isinstance(kind, str):
            parent = kind if parent is None else parent
            kind = "video"
        kind = _require_kind(kind)
        super().__init__(parent)
        self.kind = kind
        accent = _kind_label(self.kind)
        version = read_version()
        self.setWindowTitle(f"编辑{accent}上传目标与配置 · V{version}")
        self.setMinimumWidth(680)
        self.resize(720, 620 if self.kind == "video" else 540)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        target_box = QGroupBox("1 · Telegram 上传目标")
        form = QFormLayout(target_box)
        self.form = form
        form.setSpacing(10)

        self.target_mode = QComboBox()
        self.target_mode.addItem("超级群组 Forum Topic", "forum_topic")
        self.target_mode.addItem("Channel 频道", "channel")
        form.addRow("目标类型", self.target_mode)

        self.chat_id = QLineEdit()
        self.chat_id.setPlaceholderText("例如 -1001234567890")
        self.channel_chat_id = QLineEdit()
        self.channel_chat_id.setPlaceholderText("例如 -1001234567890")
        self.topic_id = QLineEdit()
        self.topic_id.setPlaceholderText("例如 1 或 42")

        form.addRow("群组 Chat ID", self.chat_id)
        form.addRow("频道 Chat ID", self.channel_chat_id)
        form.addRow("Forum Topic ID", self.topic_id)
        layout.addWidget(target_box)

        self.media_box = QGroupBox(f"2 · {accent}分组与格式选项")
        self.media_layout = QVBoxLayout(self.media_box)
        self._build_media_fields()
        layout.addWidget(self.media_box)

        hint = QLabel(
            "超级群组和频道的 Chat ID 通常以 -100 开头；频道不需要也不支持 Forum Topic。\n"
            + (
                "关闭“读取视频日期信息”后会自动按文件名自然排序和固定数量分组。"
                if self.kind == "video"
                else "混合上传按一级子文件夹分组，子文件夹内的图片和视频按同一顺序组成 Album。"
                if self.kind == "mixed"
                else "图片按修改时间或文件名自然排序；超限图片支持在上传时自动压缩临时副本。"
            )
        )
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.target_mode.currentIndexChanged.connect(self._update_fields)
        self._load_target()
        self._update_fields()

    def _build_media_fields(self):
        if self.kind == "video":
            date_box = QGroupBox("日期读取")
            date_form = QFormLayout(date_box)
            self.video_read_dates = QCheckBox("读取视频日期信息")
            self.video_read_dates.setChecked(bool(_cfg("VIDEO_READ_DATES", True)))
            self.video_read_dates.setToolTip(
                "关闭后跳过 EXIF、媒体创建日期和文件修改时间读取；"
                "视频只按文件名扫描，并按固定数量分组。"
            )
            date_form.addRow("日期读取", self.video_read_dates)

            self.video_missing_date = QComboBox()
            self.video_missing_date.addItem("使用文件修改时间", "mtime")
            self.video_missing_date.addItem("停止并提示缺失日期", "error")
            self.video_missing_date.setCurrentIndex(
                max(0, self.video_missing_date.findData(_cfg("VIDEO_MISSING_DATE_POLICY", "mtime")))
            )
            date_form.addRow("缺失日期", self.video_missing_date)

            self.video_media_creation = QCheckBox("读取媒体创建日期")
            self.video_media_creation.setChecked(
                bool(_cfg("VIDEO_READ_MEDIA_CREATION_DATE", True))
            )
            date_form.addRow("媒体日期", self.video_media_creation)
            self.media_layout.addWidget(date_box)

            group_box = QGroupBox("视频分组与标题")
            group_form = QFormLayout(group_box)
            self.video_sort = QComboBox()
            self.video_sort.addItem("按修改时间", "mtime")
            self.video_sort.addItem("按文件夹和文件名（自然数字，从小到大）", "name")
            sort_index = self.video_sort.findData(_cfg("VIDEO_SORT_MODE", "mtime"))
            self.video_sort.setCurrentIndex(sort_index if sort_index >= 0 else 0)
            group_form.addRow("扫描排序", self.video_sort)

            self.video_group_mode = QComboBox()
            self.video_group_mode.addItem("按日期分组", "date")
            self.video_group_mode.addItem("按扫描顺序固定分组", "fixed")
            group_mode = _cfg(
                "VIDEO_GROUP_MODE",
                "fixed" if _cfg("VIDEO_FORCE_TEN_PER_ALBUM", False) else "date",
            )
            mode_index = self.video_group_mode.findData(str(group_mode).lower())
            self.video_group_mode.setCurrentIndex(mode_index if mode_index >= 0 else 0)
            group_form.addRow("分组方式", self.video_group_mode)

            self.video_album = QSpinBox()
            self.video_album.setRange(1, 10)
            self.video_album.setValue(int(_cfg("VIDEO_ALBUM_SIZE", 10)))
            group_form.addRow("每组视频数", self.video_album)

            self.video_group_title = QCheckBox("带组标题（如 Album 1、25-06）")
            self.video_group_title.setChecked(bool(_cfg("VIDEO_CAPTION_INCLUDE_GROUP_TITLE", True)))
            group_form.addRow("组标题", self.video_group_title)

            self.video_filenames = QCheckBox("带文件名")
            self.video_filenames.setChecked(bool(_cfg("VIDEO_CAPTION_INCLUDE_FILENAMES", False)))
            group_form.addRow("文件名列表", self.video_filenames)

            self.video_filename_numbers = QCheckBox("文件名带序号（1、2、3…）")
            self.video_filename_numbers.setChecked(bool(_cfg("VIDEO_CAPTION_INCLUDE_FILENAME_NUMBERS", True)))
            group_form.addRow("文件名格式", self.video_filename_numbers)

            self.video_separator = QLineEdit(str(_cfg("VIDEO_ALBUM_CAPTION_SEPARATOR", " · ")))
            group_form.addRow("标题分隔符", self.video_separator)
            self.media_layout.addWidget(group_box)

            process_box = QGroupBox("上传前处理")
            process_form = QFormLayout(process_box)
            self.thumbnail = QCheckBox("生成视频缩略图")
            self.thumbnail.setChecked(bool(_cfg("VIDEO_GENERATE_THUMBNAIL", True)))
            process_form.addRow("缩略图", self.thumbnail)
            self.media_layout.addWidget(process_box)

            self.video_read_dates.toggled.connect(self._update_video_date_fields)
            self._update_video_date_fields()
        elif self.kind == "image":
            image_box = QGroupBox("图片分组与标题")
            image_form = QFormLayout(image_box)
            self.image_sort = QComboBox()
            self.image_sort.addItem("文件修改时间", "mtime")
            self.image_sort.addItem("文件夹和文件名（自然数字，从小到大）", "name")
            configured_image_sort = str(_cfg("IMAGE_SORT_MODE", "mtime")).strip().lower()
            if configured_image_sort == "path":
                configured_image_sort = "name"
            self.image_sort.setCurrentIndex(max(0, self.image_sort.findData(configured_image_sort)))
            image_form.addRow("排序方式", self.image_sort)

            self.image_album = QSpinBox()
            self.image_album.setRange(1, 10)
            self.image_album.setValue(int(_cfg("IMAGE_ALBUM_SIZE", 10)))
            image_form.addRow("每组图片数", self.image_album)

            self.image_numbering = QCheckBox("图片 Album 默认添加编号")
            self.image_numbering.setChecked(bool(_cfg("IMAGE_ALBUM_NUMBERING", True)))
            image_form.addRow("组标题", self.image_numbering)

            self.image_separator = QLineEdit(str(_cfg("IMAGE_ALBUM_CAPTION_SEPARATOR", " · ")))
            image_form.addRow("标题分隔符", self.image_separator)

            self.image_filenames = QCheckBox("图片标题附加“序号. 文件名”清单")
            self.image_filenames.setChecked(bool(_cfg("IMAGE_CAPTION_INCLUDE_FILENAMES", False)))
            image_form.addRow("文件名列表", self.image_filenames)
            self.media_layout.addWidget(image_box)

            process_box = QGroupBox("上传前处理")
            process_form = QFormLayout(process_box)
            self.image_compress = QCheckBox(
                "图片超过 10 MiB 时，在上传时使用 FFmpeg 压缩临时副本"
            )
            self.image_compress.setChecked(bool(_cfg("IMAGE_COMPRESS_OVERSIZE", False)))
            process_form.addRow("超限处理", self.image_compress)
            self.media_layout.addWidget(process_box)
        else:
            mixed_box = QGroupBox("混合分组与标题")
            mixed_form = QFormLayout(mixed_box)
            self.mixed_sort = QComboBox()
            self.mixed_sort.addItem("文件夹和文件名（自然数字，从小到大）", "name")
            self.mixed_sort.addItem("文件修改时间（从旧到新）", "mtime")
            self.mixed_sort.setCurrentIndex(
                max(0, self.mixed_sort.findData(_cfg("MIXED_SORT_MODE", "name")))
            )
            mixed_form.addRow("排序方式", self.mixed_sort)
            self.mixed_album = QSpinBox()
            self.mixed_album.setRange(1, 10)
            self.mixed_album.setValue(int(_cfg("MIXED_ALBUM_SIZE", 10)))
            mixed_form.addRow("每组媒体数", self.mixed_album)
            self.mixed_group_title = QCheckBox("带文件夹组标题")
            self.mixed_group_title.setChecked(bool(_cfg("MIXED_CAPTION_INCLUDE_GROUP_TITLE", True)))
            mixed_form.addRow("组标题", self.mixed_group_title)
            self.mixed_filenames = QCheckBox("带文件名（仅标题，不含扩展名）")
            self.mixed_filenames.setChecked(bool(_cfg("MIXED_CAPTION_INCLUDE_FILENAMES", False)))
            mixed_form.addRow("文件名列表", self.mixed_filenames)
            self.mixed_filename_numbers = QCheckBox("文件名带序号（1、2、3…）")
            self.mixed_filename_numbers.setChecked(bool(_cfg("MIXED_CAPTION_INCLUDE_FILENAME_NUMBERS", True)))
            mixed_form.addRow("文件名格式", self.mixed_filename_numbers)
            self.mixed_separator = QLineEdit(str(_cfg("MIXED_ALBUM_CAPTION_SEPARATOR", " · ")))
            mixed_form.addRow("标题分隔符", self.mixed_separator)
            self.mixed_thumbnail = QCheckBox("视频生成缩略图")
            self.mixed_thumbnail.setChecked(bool(_cfg("MIXED_GENERATE_THUMBNAIL", True)))
            mixed_form.addRow("视频处理", self.mixed_thumbnail)
            self.media_layout.addWidget(mixed_box)

    def _load_target(self):
        target = _target_for(self.kind)
        self.target_mode.blockSignals(True)
        index = self.target_mode.findData(target.get("target_mode", "forum_topic"))
        self.target_mode.setCurrentIndex(index if index >= 0 else 0)
        self.target_mode.blockSignals(False)
        self.chat_id.setText(str(target.get("group_chat_id", 0) or ""))
        self.channel_chat_id.setText(str(target.get("channel_chat_id", 0) or ""))
        self.topic_id.setText(str(target.get("forum_topic_id", 0) or ""))
        self._update_fields()

    def _update_fields(self):
        channel = self.target_mode.currentData() == "channel"
        for field, visible in (
            (self.chat_id, not channel),
            (self.channel_chat_id, channel),
            (self.topic_id, not channel),
        ):
            field.setVisible(visible)
            label = self.form.labelForField(field)
            if label is not None:
                label.setVisible(visible)

    def _update_video_date_fields(self):
        if self.kind != "video" or not hasattr(self, "video_read_dates"):
            return
        enabled = self.video_read_dates.isChecked()
        self.video_missing_date.setEnabled(enabled)
        self.video_media_creation.setEnabled(enabled)
        self.video_sort.setEnabled(enabled)
        self.video_group_mode.setEnabled(enabled)
        if not enabled:
            name_index = self.video_sort.findData("name")
            fixed_index = self.video_group_mode.findData("fixed")
            if name_index >= 0:
                self.video_sort.setCurrentIndex(name_index)
            if fixed_index >= 0:
                self.video_group_mode.setCurrentIndex(fixed_index)

    def _save(self):
        try:
            group_id = int(self.chat_id.text().strip() or "0")
            channel_id = int(self.channel_chat_id.text().strip() or "0")
            topic_id = int(self.topic_id.text().strip() or "0")
        except ValueError:
            QMessageBox.critical(self, "保存失败", "Chat ID 和 Topic ID 都必须是整数。")
            return
        mode = self.target_mode.currentData() or "forum_topic"
        if mode == "channel":
            if channel_id == 0:
                QMessageBox.critical(self, "保存失败", "频道 Chat ID 不能为 0。")
                return
        else:
            if group_id == 0:
                QMessageBox.critical(self, "保存失败", "群组 Chat ID 不能为 0。")
                return
            if topic_id <= 0:
                QMessageBox.critical(self, "保存失败", "Forum Topic ID 必须大于 0。")
                return
        values = {
            (f"telegram.{self.kind}", "target_mode"): mode,
            (f"telegram.{self.kind}", "chat_id"): group_id,
            (f"telegram.{self.kind}", "channel_chat_id"): channel_id,
            (f"telegram.{self.kind}", "forum_topic_id"): topic_id,
        }
        if self.kind == "video":
            read_dates = self.video_read_dates.isChecked()
            values.update({
                ("video", "missing_date_policy"): self.video_missing_date.currentData(),
                ("video", "read_media_creation_date"): self.video_media_creation.isChecked(),
                ("video", "read_dates"): read_dates,
                ("video", "sort_mode"): self.video_sort.currentData() if read_dates else "name",
                ("video", "album_size"): self.video_album.value(),
                ("video", "group_mode"): self.video_group_mode.currentData() if read_dates else "fixed",
                ("video", "force_ten_per_album"): (
                    self.video_group_mode.currentData() == "fixed"
                    if read_dates
                    else True
                ),
                ("video", "caption_include_group_title"): self.video_group_title.isChecked(),
                ("video", "album_caption_separator"): self.video_separator.text(),
                ("video", "caption_include_filenames"): self.video_filenames.isChecked(),
                ("video", "caption_include_filename_numbers"): self.video_filename_numbers.isChecked(),
                ("video", "generate_thumbnail"): self.thumbnail.isChecked(),
            })
        elif self.kind == "image":
            values.update({
                ("image", "sort_mode"): self.image_sort.currentData(),
                ("image", "album_size"): self.image_album.value(),
                ("image", "album_numbering"): self.image_numbering.isChecked(),
                ("image", "album_caption_separator"): self.image_separator.text(),
                ("image", "caption_include_filenames"): self.image_filenames.isChecked(),
                ("image", "compress_oversize"): self.image_compress.isChecked(),
            })
        else:
            values.update({
                ("mixed", "sort_mode"): self.mixed_sort.currentData(),
                ("mixed", "album_size"): self.mixed_album.value(),
                ("mixed", "caption_include_group_title"): self.mixed_group_title.isChecked(),
                ("mixed", "caption_include_filenames"): self.mixed_filenames.isChecked(),
                ("mixed", "caption_include_filename_numbers"): self.mixed_filename_numbers.isChecked(),
                ("mixed", "album_caption_separator"): self.mixed_separator.text(),
                ("mixed", "generate_thumbnail"): self.mixed_thumbnail.isChecked(),
            })
        error = _write_config_values(values)
        if error:
            QMessageBox.critical(self, "保存失败", error)
            return
        self.accept()


__all__ = ["TargetDialog"]
