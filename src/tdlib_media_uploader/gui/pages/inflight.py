# -*- coding: utf-8 -*-
"""Review unconfirmed Telegram uploads by Album."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

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
            "请先在 Telegram 核对整组文件。整组已发送时选择“标记为已发送”，需要补传时选择“允许重新上传”。"
        )
        banner_text.setWordWrap(True)
        banner_text.setObjectName("mutedLabel")
        banner_layout.addWidget(banner_text)
        layout.addWidget(banner)

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
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        # Action buttons
        buttons = QHBoxLayout()
        refresh = QPushButton("刷新列表")
        refresh.clicked.connect(self.reload_records)

        self.sent_button = QPushButton("标记为已发送")
        self.sent_button.setObjectName("primaryButton")
        self.sent_button.clicked.connect(lambda: self._emit_choice(True))

        self.not_sent_button = QPushButton("允许重新上传")
        self.not_sent_button.setObjectName("dangerButton")
        self.not_sent_button.clicked.connect(lambda: self._emit_choice(False))

        buttons.addWidget(refresh)
        buttons.addStretch(1)
        buttons.addWidget(self.sent_button)
        buttons.addWidget(self.not_sent_button)
        layout.addLayout(buttons)

        self.table.itemSelectionChanged.connect(self._update_actions)
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

    def _selected_record(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        value = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, dict) else None

    def _update_actions(self):
        record = self._selected_record()
        status = str(record.get("status", "")).upper() if record else ""
        self.sent_button.setEnabled(record is not None and status != "CORRUPT")
        self.not_sent_button.setEnabled(status in {"PREPARED", "SUBMITTED", "UNKNOWN"})

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
            "请确认你已经核对 Telegram 中的目标和整个 Album。"
        )
        if not sent:
            message += "\n\n下次上传将跳过日志中已确认成功的文件。请确认其余文件可以重新上传。"
            if record.get("succeeded_ids") and not record.get("item_results"):
                message += "\n旧日志无法区分已发送的文件，整组重新上传可能产生重复消息。"
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
            self._update_actions()
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
        self.hint.setText(
            f"当前有 {len(records)} 条未确认记录。处理“已发送”前必须先核对 Telegram。"
            if records else "没有未确认上传记录。"
        )

    def refresh_theme(self):
        """Re-render inflight view on theme switch."""
        self.reload_records()


__all__ = ["InflightPage"]
