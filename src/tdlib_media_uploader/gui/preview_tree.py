"""Scan preview model with stable item indexes and in-place status updates."""

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QFont

from .theme import status_color


class PreviewRow:
    __slots__ = (
        "children",
        "parent",
        "position",
        "values",
        "tooltips",
        "plan",
        "delivery_status",
        "excluded",
    )

    def __init__(
        self,
        values=(),
        parent=None,
        *,
        plan=None,
        delivery_status: str = "",
        excluded: bool = False,
    ):
        self.children = []
        self.parent = parent
        self.position = len(parent.children) if parent is not None else 0
        self.values = list(values)
        self.tooltips = {}
        self.plan = plan
        self.delivery_status = str(delivery_status or (values[0] if values else ""))
        self.excluded = bool(excluded)
        if parent is not None:
            parent.children.append(self)


class PreviewTreeModel(QAbstractItemModel):
    headers = ("状态", "日期 / 分组", "大小", "文件路径")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.root = PreviewRow()

    def set_root(self, root=None):
        self.beginResetModel()
        self.root = root if root is not None else PreviewRow()
        self.endResetModel()

    def row(self, index):
        return index.internalPointer() if index.isValid() else self.root

    def rowCount(self, parent=QModelIndex()):
        return len(self.row(parent).children) if parent.column() <= 0 else 0

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        return self.createIndex(row, column, self.row(parent).children[row])

    def parent(self, index):
        if index.isValid():
            parent = self.row(index).parent
            if parent is not self.root:
                return self.createIndex(parent.position, 0, parent)
        return QModelIndex()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.row(index)
        if role == Qt.ItemDataRole.DisplayRole:
            return row.values[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.tooltips.get(index.column())
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 0:
            label = str(row.values[0] or "")
            if label and label != "分组":
                return QColor(status_color(label))
        if role == Qt.ItemDataRole.FontRole and row.values[0] == "分组" and index.column() in (0, 1):
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.UserRole and index.column() == 0:
            return row.plan
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    def update_row(
        self,
        row,
        *,
        text=None,
        tooltips=None,
        delivery_status=None,
        excluded=None,
    ):
        changed = set()
        roles = []
        if delivery_status is not None:
            row.delivery_status = str(delivery_status)
        if excluded is not None:
            row.excluded = bool(excluded)
        for column, value in (text or {}).items():
            if row.values[column] != value:
                row.values[column] = value
                changed.add(column)
        if changed:
            roles.append(Qt.ItemDataRole.DisplayRole)
        changed_tooltips = set()
        for column, value in (tooltips or {}).items():
            if row.tooltips.get(column) != value:
                row.tooltips[column] = value
                changed_tooltips.add(column)
        if changed_tooltips:
            roles.append(Qt.ItemDataRole.ToolTipRole)
            changed.update(changed_tooltips)
        if changed:
            self.dataChanged.emit(self.createIndex(row.position, min(changed), row),
                                  self.createIndex(row.position, max(changed), row), roles)


class PreviewTreeFilter(QSortFilterProxyModel):
    """Accept matching leaves and their ancestors, keeping pending-only semantics."""

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self._visible = None
        self.setDynamicSortFilter(False)
        self.setSourceModel(source)

    @staticmethod
    def _row_matches_query(row: PreviewRow, query: str) -> bool:
        if not query:
            return True
        return (
            query in " ".join(row.values).casefold()
            or query in row.tooltips.get(1, "").casefold()
            or query in row.tooltips.get(3, "").casefold()
        )

    @staticmethod
    def _leaf_visible(row: PreviewRow, match: bool, pending_only: bool) -> bool:
        status = row.delivery_status or row.values[0]
        return match and (
            not pending_only
            or (not row.excluded and status not in {"已完成", "已跳过"})
        )

    def _visit_subtree(self, row: PreviewRow, inherited: bool, query: str, pending_only: bool, visible: set):
        match = inherited or self._row_matches_query(row, query)
        if row.children:
            accepted = False
            for child in row.children:
                accepted = self._visit_subtree(child, match, query, pending_only, visible) or accepted
        else:
            accepted = self._leaf_visible(row, match, pending_only)
        if accepted:
            visible.add(row)
        return accepted

    def set_filters(self, query, pending_only):
        if not query and not pending_only and self._visible is None:
            return
        self.beginFilterChange()
        if query or pending_only:
            visible = set()
            for row in self.sourceModel().root.children:
                self._visit_subtree(row, False, query, pending_only, visible)
            self._visible = visible
        else:
            self._visible = None
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def update_album_rows(self, album_rows, query: str, pending_only: bool) -> None:
        """Incrementally refresh filter visibility for touched Albums when active."""
        if not query and not pending_only:
            if self._visible is not None:
                self.set_filters("", False)
            return
        if self._visible is None or not album_rows:
            self.set_filters(query, pending_only)
            return

        root = self.sourceModel().root
        to_add: set[PreviewRow] = set()
        to_remove: set[PreviewRow] = set()
        touched_groups: set[PreviewRow] = set()

        for album_row in album_rows:
            parent = album_row.parent
            group_inherited = (
                self._row_matches_query(parent, query)
                if parent is not None and parent is not root
                else False
            )
            sub_visible: set[PreviewRow] = set()
            self._visit_subtree(album_row, group_inherited, query, pending_only, sub_visible)

            subtree_nodes = [album_row, *album_row.children]
            for node in subtree_nodes:
                now_vis = node in sub_visible
                was_vis = node in self._visible
                if now_vis and not was_vis:
                    to_add.add(node)
                elif was_vis and not now_vis:
                    to_remove.add(node)
            if parent is not None and parent is not root:
                touched_groups.add(parent)

        for group_row in touched_groups:
            group_vis = any(
                (child in to_add) or (child in self._visible and child not in to_remove)
                for child in group_row.children
            )
            was_vis = group_row in self._visible
            if group_vis and not was_vis:
                to_add.add(group_row)
            elif was_vis and not group_vis:
                to_remove.add(group_row)

        if not to_add and not to_remove:
            return
        self.beginFilterChange()
        self._visible.difference_update(to_remove)
        self._visible.update(to_add)
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row, source_parent):
        return (self._visible is None
                or self.sourceModel().row(source_parent).children[source_row] in self._visible)
