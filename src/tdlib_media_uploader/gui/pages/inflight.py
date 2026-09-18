# -*- coding: utf-8 -*-
"""Modern unconfirmed upload (inflight reconciliation) page for Beta 3."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..theme import THEME
from ..tools import kind_label as _kind_label


class InflightPage(QWidget):
    """Review send attempts whose Telegram result was not confirmed."""

    reconciliation_requested = Signal(object, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        # Header
        title_row = QHBoxLayout()
        title = QLabel("未确认上传对账中心")
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)

        self.hint = QLabel("这些记录可能已经发送到 Telegram，请先核对目标中的 Album。")
        self.hint.setObjectName("mutedLabel")
        title_row.addWidget(self.hint)
        layout.addLayout(title_row)

        # Explanation banner
        banner = QFrame()
        banner.setObjectName("bannerFrame")
        banner_layout = QVBoxLayout(banner)
        banner_layout.setContentsMargins(14, 10, 14, 10)
        banner_text = QLabel(
            "当网络波动、超时或强制中断时，TDLib 可能已成功向 Telegram 提交了媒体组，但尚未收到服务端的确认回复。"
            "为了防止重复发送，系统会将批次转入“未确认状态”（UNKNOWN）。\n"
            "请前往对应频道或群组核验该批文件：如已成功发送，点击“Telegram 中存在”以修复断点；如未发送，点击“Telegram 中不存在”以便重新上传。"
        )
        banner_text.setWordWrap(True)
        banner_text.setObjectName("mutedLabel")
        banner_layout.addWidget(banner_text)
        layout.addWidget(banner)

        # Splitter: Table on top, File details drawer on bottom
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Table container
        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(8)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            [
                "媒体类型",
                "文件/Album 摘要",
                "状态",
                "创建时间",
                "最后更新",
                "Telegram 目标",
                "源文件目录/路径",
                "文件数",
                "错误信息",
            ]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.cellDoubleClicked.connect(lambda row, _column: self._show_details(row))
        self.table.horizontalHeader().setStretchLastSection(True)
        table_layout.addWidget(self.table)
        splitter.addWidget(table_container)

        # Detail drawer container
        drawer_container = QFrame()
        drawer_container.setObjectName("bannerFrame")
        drawer_layout = QVBoxLayout(drawer_container)
        drawer_layout.setContentsMargins(12, 10, 12, 10)
        drawer_layout.setSpacing(6)

        drawer_header = QHBoxLayout()
        self.drawer_title = QLabel("选中记录包含的源文件")
        self.drawer_title.setObjectName("valueLabel")
        drawer_header.addWidget(self.drawer_title)
        drawer_header.addStretch(1)
        drawer_layout.addLayout(drawer_header)

        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(140)
        drawer_layout.addWidget(self.file_list)
        splitter.addWidget(drawer_container)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        # Action buttons
        buttons = QHBoxLayout()
        refresh = QPushButton("刷新列表")
        refresh.clicked.connect(self.reload_records)

        self.details_button = QPushButton("查看完整文件列表")
        self.details_button.clicked.connect(self._show_selected_details)
        self.details_button.setEnabled(False)

        self.sent_button = QPushButton("我已确认 Telegram 中存在")
        self.sent_button.setObjectName("primaryButton")
        self.sent_button.clicked.connect(lambda: self._emit_choice(True))

        self.not_sent_button = QPushButton("我已确认 Telegram 中不存在")
        self.not_sent_button.setObjectName("dangerButton")
        self.not_sent_button.clicked.connect(lambda: self._emit_choice(False))

        buttons.addWidget(refresh)
        buttons.addWidget(self.details_button)
        buttons.addStretch(1)
        buttons.addWidget(self.sent_button)
        buttons.addWidget(self.not_sent_button)
        layout.addLayout(buttons)

        self.table.itemSelectionChanged.connect(self._update_actions)
        self.table.itemSelectionChanged.connect(self._update_drawer)
        self.reload_records()

    @staticmethod
    def _item_paths(record: Mapping) -> list[str]:
        values = record.get("items", []) if isinstance(record, Mapping) else []
        if not isinstance(values, list):
            return []
        paths: list[str] = []
        for raw in values:
            value = raw.get("path") if isinstance(raw, Mapping) else raw
            if value not in (None, ""):
                paths.append(str(value))
        return paths

    @staticmethod
    def _filename(path: str) -> str:
        return (PureWindowsPath(path).name if "\\" in path else Path(path).name) or path

    @classmethod
    def _summary(cls, record: Mapping) -> str:
        paths = cls._item_paths(record)
        if not paths:
            return (
                "损坏的上传记录"
                if str(record.get("status", "")).upper() == "CORRUPT"
                else "旧版记录，缺少源文件信息"
            )
        if len(paths) == 1:
            return cls._filename(paths[0])
        return f"{cls._filename(paths[0])} 等 {len(paths)} 个文件"

    @staticmethod
    def _source_text(paths: list[str]) -> str:
        if not paths:
            return "旧版记录，缺少源文件信息"
        first = paths[0]
        parent = PureWindowsPath(first).parent if "\\" in first else Path(first).parent
        parents = {
            str(PureWindowsPath(path).parent if "\\" in path else Path(path).parent)
            for path in paths
        }
        if len(parents) == 1:
            return str(parent)
        return f"多个目录（首个：{parent}）"

    @staticmethod
    def _status_text(status: str) -> str:
        return {
            "PREPARED": "待确认 / 尚未完成发送",
            "SUBMITTED": "已提交 / 等待确认",
            "UNKNOWN": "未确认 / 禁止自动重试",
            "CONFIRMED": "Telegram 已确认 / 本地断点待修复",
            "CORRUPT": "损坏的上传记录",
        }.get(str(status).upper(), str(status or "未知"))

    @staticmethod
    def _target_text(record: Mapping) -> str:
        from ...core.upload_journal import _record_target

        target = _record_target(dict(record))
        if not target:
            return "⚠ 旧版记录：目标未知"
        mode = str(target.get("target_mode", "")).lower()
        chat_id = target.get("chat_id", "")
        if mode == "channel":
            return f"频道 · Chat ID {chat_id}"
        return f"群组 · Chat ID {chat_id} · Topic {target.get('forum_topic_id', '')}"

    @classmethod
    def _detail_text(cls, record: Mapping) -> str:
        paths = cls._item_paths(record)
        lines = [
            f"媒体类型：{_kind_label(record.get('kind', 'unknown'))}",
            f"状态：{cls._status_text(str(record.get('status', '')))}",
            f"Telegram 目标：{cls._target_text(record)}",
            f"文件数量：{len(paths)}" if paths else "文件数量：未知",
            f"内部 Album ID：{record.get('album_key', '未知')}",
        ]
        if paths:
            lines.append("源文件：")
            lines.extend(f"{index}. {path}" for index, path in enumerate(paths, start=1))
        else:
            lines.append("源文件：旧版记录，缺少源文件信息")
        if record.get("journal_path"):
            lines.append(f"journal 文件：{record['journal_path']}")
        if record.get("error"):
            lines.append(f"错误信息：{record['error']}")
        return "\n".join(lines)

    def _selected_record(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, dict) else None

    def _update_drawer(self):
        record = self._selected_record()
        self.file_list.clear()
        if not record:
            self.drawer_title.setText("未选中记录")
            return
        paths = self._item_paths(record)
        self.drawer_title.setText(f"包含 {len(paths)} 个文件（Album ID: {record.get('album_key', '')}）")
        for p in paths:
            self.file_list.addItem(p)

    def _update_actions(self):
        record = self._selected_record()
        status = str(record.get("status", "")).upper() if record else ""
        corrupt = status == "CORRUPT"
        confirmed = status == "CONFIRMED"
        self.details_button.setEnabled(record is not None)
        self.sent_button.setEnabled(record is not None and not corrupt)
        self.sent_button.setText("修复本地断点" if confirmed else "我已确认 Telegram 中存在")
        self.not_sent_button.setEnabled(
            record is not None and status in {"PREPARED", "SUBMITTED", "UNKNOWN"}
        )

    def _show_details(self, row: int):
        item = self.table.item(row, 0)
        record = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not isinstance(record, dict):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("未确认上传详情")
        dialog.resize(760, 520)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(self._detail_text(record))
        layout.addWidget(text)
        close = QPushButton("关闭")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def _show_selected_details(self):
        row = self.table.currentRow()
        if row >= 0:
            self._show_details(row)

    def _emit_choice(self, sent: bool):
        record = self._selected_record()
        if record is None:
            QMessageBox.information(self, "请选择记录", "请先选择一条未确认上传记录。")
            return
        status = str(record.get("status", "")).upper()
        if status == "CORRUPT":
            QMessageBox.warning(
                self,
                "记录损坏",
                f"这条记录无法安全处理，文件已保留：\n{record.get('journal_path', '')}",
            )
            return
        if not sent and status == "CONFIRMED":
            return
        action = (
            "修复本地断点并清理保护记录"
            if status == "CONFIRMED"
            else "标记为 Telegram 已发送"
            if sent
            else "允许下次重新发送"
        )
        target = self._target_text(record)
        summary = self._summary(record)
        paths = self._item_paths(record)
        message = (
            f"将{action}：\n"
            f"文件/Album：{summary}\n"
            f"状态：{self._status_text(status)}\n"
            f"源文件：{self._source_text(paths)}\n"
            f"文件数量：{len(paths) if paths else '未知'}\n"
            f"Telegram 目标：{target}\n\n"
            "请确认你已经核对 Telegram 中的目标和 Album。"
        )
        if sent and target.startswith("⚠"):
            message += (
                "\n\n这是一条旧版未记录 Telegram 目标的上传记录。"
                "\n程序无法确认它当时发送到哪个群组/Topic/频道。"
                "\n\n只有在你确认“当前配置的 Telegram 目标”就是当时发送该 Album 的目标时，"
                "才能选择“已发送”。"
                "\n\n如果目标不一致，请不要确认已发送。"
            )
        answer = QMessageBox.warning(
            self,
            "确认人工处理",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.reconciliation_requested.emit(record, sent)

    def reload_records(self):
        try:
            from ...core.upload_journal import InflightJournal, UNRESOLVED

            records = [
                record for record in InflightJournal().list_unresolved(refresh=True)
                if str(record.get("status", "")).upper() in UNRESOLVED
            ]
        except Exception as exc:
            self.table.setRowCount(0)
            self.hint.setText(f"读取未确认记录失败：{exc}")
            self._update_drawer()
            return
        self.table.setRowCount(len(records))
        for row, record in enumerate(records):
            paths = self._item_paths(record)
            values = [
                _kind_label(record.get("kind", "unknown")),
                self._summary(record),
                self._status_text(str(record.get("status", ""))),
                record.get("created_at", ""),
                record.get("updated_at", ""),
                self._target_text(record),
                self._source_text(paths),
                len(paths) if paths else "未知",
                record.get("error", ""),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, record)
                self.table.setItem(row, column, cell)
        self.table.resizeColumnsToContents()
        self._update_actions()
        self._update_drawer()
        self.hint.setText(
            f"当前有 {len(records)} 条未确认记录。处理“已发送”前必须先核对 Telegram。"
            if records else "没有未确认上传记录。"
        )


__all__ = ["InflightPage"]
