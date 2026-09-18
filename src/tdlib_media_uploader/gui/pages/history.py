# -*- coding: utf-8 -*-
"""Modern upload history page with metrics and filtering for Beta 3."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..components.cards import StatCard
from ..theme import THEME


def _kind_label(kind: str) -> str:
    from ..main_window import _kind_label as get_label

    return get_label(kind)


def _load_history() -> list[dict]:
    from ..main_window import _load_history as load_hist

    return load_hist()


def _save_history(records: list[dict]) -> None:
    from ..main_window import _save_history as save_hist

    return save_hist(records)


def _fmt_size(value: float | int | None) -> str:
    from ..main_window import _fmt_size as fmt_s

    return fmt_s(value)


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

        # Summary Metric Cards
        metrics_layout = QGridLayout()
        metrics_layout.setSpacing(12)
        self.total_runs_card = StatCard("历史任务总数", "0", "累计上传批次")
        self.total_bytes_card = StatCard("累计上传数据量", "0 B", "成功与中断总计")
        self.success_rate_card = StatCard("任务成功率", "—", "成功完成比例")

        metrics_layout.addWidget(self.total_runs_card, 0, 0)
        metrics_layout.addWidget(self.total_bytes_card, 0, 1)
        metrics_layout.addWidget(self.success_rate_card, 0, 2)
        layout.addLayout(metrics_layout)

        # Filter bar
        filter_bar = QHBoxLayout()
        filter_bar.setSpacing(10)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("按来源目录或错误信息搜索…")
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
        self.status_filter.addItem("停止 / 失败", "failed")
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
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        layout.addWidget(self.table, 1)

        self._all_records: list[dict] = []
        self.reload_records()

    def reload_records(self):
        self._all_records = list(reversed(_load_history()))
        self._update_metrics(self._all_records)
        self._apply_filter()

    def _update_metrics(self, records: list[dict]):
        total_runs = len(records)
        total_bytes = sum(int(r.get("total_bytes", 0) or 0) for r in records)
        successful_runs = sum(1 for r in records if r.get("success"))

        self.total_runs_card.set_value(f"{total_runs} 次")
        self.total_bytes_card.set_value(_fmt_size(total_bytes))
        if total_runs > 0:
            pct = (successful_runs / total_runs) * 100
            self.success_rate_card.set_value(f"{pct:.1f}%", good=pct >= 80)
        else:
            self.success_rate_card.set_value("—")

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
            values = [
                record.get("finished_at", record.get("started_at", "")),
                _kind_label(record.get("kind", "image")),
                record.get("source_dir", ""),
                str(record.get("total_files", 0)),
                _fmt_size(record.get("total_bytes", 0)),
                "成功" if is_success else "停止/失败",
                record.get("message", ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 5:
                    item.setForeground(Qt.GlobalColor.green if is_success else Qt.GlobalColor.yellow)
                self.table.setItem(row, column, item)

        self.table.resizeColumnsToContents()

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
