# -*- coding: utf-8 -*-
"""Modern upload history page with metrics and filtering for Beta 4."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..components.cards import StatCard
from ..history_service import load_history, save_history
from ..theme import status_color
from ..tools import (
    apply_capped_column_widths,
    existing_directory_for,
    format_size,
    kind_label,
    open_in_file_manager,
)


_kind_label = kind_label
_load_history = load_history
_save_history = save_history
_fmt_size = format_size


class HistoryPage(QWidget):
    """Modern upload session history viewer with aggregated metrics and filter controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        # Header
        header = QHBoxLayout()
        title = QLabel("历史上传记录")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        header.addStretch(1)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.reload_records)
        clear_btn = QPushButton("清空历史")
        clear_btn.setObjectName("dangerButton")
        clear_btn.clicked.connect(self._clear_history)
        header.addWidget(refresh_btn)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        # Summary Metric Cards: 历史任务数, 已上传文件数, 累计数据量
        metrics_layout = QGridLayout()
        metrics_layout.setSpacing(12)
        self.total_runs_card = StatCard("历史任务数", "0 次", "累计执行批次")
        self.total_files_card = StatCard("已上传文件数", "0 个", "累计处理文件")
        self.total_bytes_card = StatCard("累计数据量", "0 B", "总传输体积")

        metrics_layout.addWidget(self.total_runs_card, 0, 0)
        metrics_layout.addWidget(self.total_files_card, 0, 1)
        metrics_layout.addWidget(self.total_bytes_card, 0, 2)
        layout.addLayout(metrics_layout)

        # Filter bar
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(10)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("按来源目录或说明搜索…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._apply_filter)
        filter_bar.addWidget(self.search_edit, 1)

        self.kind_filter = QComboBox()
        self.kind_filter.addItem("全部类型", "all")
        self.kind_filter.addItem("视频", "video")
        self.kind_filter.addItem("图片", "image")
        self.kind_filter.addItem("混合", "mixed")
        self.kind_filter.currentIndexChanged.connect(self._apply_filter)
        filter_bar.addWidget(self.kind_filter)

        self.status_filter = QComboBox()
        self.status_filter.addItem("全部结果", "all")
        self.status_filter.addItem("仅成功", "success")
        self.status_filter.addItem("未完成", "failed")
        self.status_filter.currentIndexChanged.connect(self._apply_filter)
        filter_bar.addWidget(self.status_filter)

        layout.addLayout(filter_bar)

        # Main Table
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["时间", "类型", "来源", "文件", "数据量", "结果", "说明"])
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.cellDoubleClicked.connect(lambda _r, _c: self._show_selected_details())
        layout.addWidget(self.table, 1)

        self._all_records: list[dict] = []
        self.reload_records()

    def reload_records(self):
        self._all_records = list(reversed(_load_history()))
        self._update_metrics(self._all_records)
        self._apply_filter()

    def _update_metrics(self, records: list[dict]):
        total_runs = len(records)
        total_files = sum(int(r.get("total_files", 0) or 0) for r in records)
        total_bytes = sum(int(r.get("total_bytes", 0) or 0) for r in records)

        self.total_runs_card.set_value(f"{total_runs} 次")
        self.total_files_card.set_value(f"{total_files} 个")
        self.total_bytes_card.set_value(_fmt_size(total_bytes))

    def _apply_filter(self):
        query = self.search_edit.text().strip().lower()
        kind = self.kind_filter.currentData()
        status_mode = self.status_filter.currentData()

        filtered = []
        for r in self._all_records:
            if kind != "all" and str(r.get("kind", "")).lower() != kind:
                continue
            is_success = bool(r.get("success"))
            if status_mode == "success" and not is_success:
                continue
            if status_mode == "failed" and is_success:
                continue
            if query:
                source = str(r.get("source_dir", "")).lower()
                msg = str(r.get("message", "")).lower()
                if query not in source and query not in msg:
                    continue
            filtered.append(r)

        self.table.setRowCount(len(filtered))
        for row, record in enumerate(filtered):
            is_success = bool(record.get("success"))
            raw_status = str(record.get("status", ""))
            status = "成功" if is_success else "停止/失败"
            if raw_status == "SKIPPED":
                status = "已跳过"
            values = [
                record.get("finished_at", record.get("started_at", "")),
                _kind_label(record.get("kind", "image")),
                record.get("source_dir", ""),
                str(record.get("total_files", 0)),
                _fmt_size(record.get("total_bytes", 0)),
                status,
                record.get("message", ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, record)
                if column == 5:
                    color_key = "SKIPPED" if raw_status == "SKIPPED" else ("SUCCESS" if is_success else "WARNING")
                    item.setForeground(QColor(status_color(color_key)))
                self.table.setItem(row, column, item)

        apply_capped_column_widths(
            self.table,
            max_widths={0: 175, 2: 280, 6: 320},
            min_widths={1: 68, 3: 64, 4: 86, 5: 84},
        )

    def _selected_record(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, dict) else None

    def _show_context_menu(self, pos):
        record = self._selected_record()
        if record is None:
            return
        source_dir = str(record.get("source_dir", "") or "")
        menu = QMenu(self)
        detail_action = menu.addAction("查看记录详情")
        open_action = menu.addAction("打开来源目录")
        open_action.setEnabled(existing_directory_for(source_dir) is not None)
        copy_action = menu.addAction("复制来源路径")
        copy_action.setEnabled(bool(source_dir))
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == detail_action:
            self._show_selected_details()
        elif chosen == open_action:
            open_in_file_manager(source_dir)
        elif chosen == copy_action:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(source_dir)

    def _show_selected_details(self):
        record = self._selected_record()
        if record is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("历史上传记录详情")
        dialog.setMinimumWidth(540)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        source_dir = str(record.get("source_dir", "") or "")
        status_label = "已跳过" if record.get("status") == "SKIPPED" else (
            "成功" if record.get("success") else "停止/失败"
        )
        summary = QLabel(
            f"媒体类型：{_kind_label(record.get('kind', ''))}\n"
            f"结果：{status_label}\n"
            f"开始时间：{record.get('started_at', '—')}\n"
            f"结束时间：{record.get('finished_at', '—')}\n"
            f"文件数：{record.get('total_files', 0)} · 总大小：{_fmt_size(record.get('total_bytes', 0))}\n"
            f"来源目录：{source_dir or '—'}"
        )
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(summary)

        detail_box = QPlainTextEdit(str(record.get("message", "") or "无附加说明"))
        detail_box.setReadOnly(True)
        detail_box.setMaximumHeight(160)
        layout.addWidget(detail_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        if existing_directory_for(source_dir) is not None:
            open_btn = buttons.addButton("打开来源目录", QDialogButtonBox.ButtonRole.ActionRole)
            open_btn.clicked.connect(lambda: open_in_file_manager(source_dir))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def refresh_theme(self):
        """Update table foreground colors on theme change."""
        self._apply_filter()

    def _clear_history(self):
        if not self._all_records:
            return
        ans = QMessageBox.question(
            self,
            "确认清空历史",
            "确定要清空所有历史上传记录吗？此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            _save_history([])
            self.reload_records()


__all__ = ["HistoryPage"]
