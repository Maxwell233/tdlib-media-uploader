"""Scan preview model with stable item indexes and in-place status updates."""

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QFont

from .theme import status_color


class PreviewRow:
    __slots__ = ("children", "parent", "position", "values", "tooltips", "plan")

    def __init__(self, values=(), parent=None, *, plan=None):
        self.children = []
        self.parent = parent
        self.position = len(parent.children) if parent is not None else 0
        self.values = list(values)
        self.tooltips = {}
        self.plan = plan
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

    def update_row(self, row, *, text=None, tooltips=None):
        changed = set()
        roles = []
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

    def set_filters(self, query, pending_only):
        if not query and not pending_only and self._visible is None:
            return
        self.beginFilterChange()
        if query or pending_only:
            visible = set()

            def visit(row, inherited=False):
                match = (
                    inherited
                    or not query
                    or query in " ".join(row.values).casefold()
                    or query in row.tooltips.get(1, "").casefold()
                    or query in row.tooltips.get(3, "").casefold()
                )
                if row.children:
                    accepted = False
                    for child in row.children:
                        accepted = visit(child, match) or accepted
                else:
                    accepted = match and (not pending_only or row.values[0] not in {"已完成", "已跳过"})
                if accepted:
                    visible.add(row)
                return accepted

            for row in self.sourceModel().root.children:
                visit(row)
            self._visible = visible
        else:
            self._visible = None
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row, source_parent):
        return (self._visible is None
                or self.sourceModel().row(source_parent).children[source_row] in self._visible)
