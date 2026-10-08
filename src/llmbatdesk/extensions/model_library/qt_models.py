from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import QApplication

from ...qt.theme import model_role_color
from .gguf_reader import parameter_scale
from .models import (
    AssociatedComponent, ModelFileRole, ModelRecord, ModelStatus, ParseStatus,
    ReferenceRole, ReferenceState, ScanRoot, ScriptReference,
)
from .presentation import is_hidden_auxiliary


MODEL_ROLE = int(Qt.ItemDataRole.UserRole) + 1
ROOT_ROLE = MODEL_ROLE + 1
REFERENCE_ROLE = ROOT_ROLE + 1
COMPONENT_ROLE = REFERENCE_ROLE + 1
MODEL_FILE_ROLE_ROLE = COMPONENT_ROLE + 1

PARSE_STATUS_LABELS = {
    ParseStatus.PARSED: "已解析",
    ParseStatus.UNSUPPORTED_METADATA: "含不支持的元数据",
    ParseStatus.LIMITED: "元数据受限",
    ParseStatus.UNSUPPORTED_VERSION: "GGUF 版本不支持",
    ParseStatus.MALFORMED: "格式错误或文件截断",
    ParseStatus.SAFETY_LIMIT: "超过安全限制",
    ParseStatus.IO_ERROR: "I/O 错误",
    ParseStatus.ERROR: "读取错误",
    ParseStatus.UNKNOWN: "未知",
}
ROLE_LABELS = {
    ReferenceRole.PRIMARY: "主模型",
    ReferenceRole.MMPROJ: "多模态投影",
    ReferenceRole.DRAFT: "草稿模型",
    ReferenceRole.ADAPTER: "适配器",
    ReferenceRole.CONTROL: "控制文件",
    ReferenceRole.AUXILIARY: "其他辅助文件",
    ReferenceRole.DYNAMIC: "动态，无法确认",
    ReferenceRole.AMBIGUOUS: "歧义候选",
}
REFERENCE_STATE_LABELS = {
    ReferenceState.EXPLICIT: "明确引用",
    ReferenceState.MISSING: "文件缺失",
    ReferenceState.MULTIPLE: "多个候选模型",
    ReferenceState.AMBIGUOUS: "歧义",
    ReferenceState.DYNAMIC: "动态",
    ReferenceState.NO_LOCAL_GGUF: "不使用本地 GGUF",
    ReferenceState.UNPARSED: "未解析",
}
MODEL_FILE_ROLE_LABELS = {
    ModelFileRole.PRIMARY: "主模型",
    ModelFileRole.MMPROJ: "多模态投影",
    ModelFileRole.DRAFT: "草稿/MTP 模型",
    ModelFileRole.ADAPTER: "适配器/LoRA",
    ModelFileRole.CONTROL: "控制文件",
    ModelFileRole.EMBEDDING: "嵌入或重排模型",
    ModelFileRole.AUXILIARY: "其他辅助 GGUF",
    ModelFileRole.UNKNOWN: "角色未知",
}
MODEL_FILE_ROLE_MARKERS = {
    ModelFileRole.MMPROJ: "〔投影〕",
    ModelFileRole.DRAFT: "〔草稿〕",
    ModelFileRole.ADAPTER: "〔适配〕",
    ModelFileRole.CONTROL: "〔控制〕",
    ModelFileRole.EMBEDDING: "〔嵌入〕",
    ModelFileRole.AUXILIARY: "〔附属〕",
    ModelFileRole.UNKNOWN: "〔未知〕",
}


class RootTableModel(QAbstractTableModel):
    HEADERS = ("目录", "启用", "递归", "状态")

    def __init__(self, update_callback=None, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[ScanRoot] = []
        self._update_callback = update_callback

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        root = self.rows[index.row()]
        if role == ROOT_ROLE:
            return root
        if role == Qt.ItemDataRole.UserRole:
            return root.root_id
        if role == Qt.ItemDataRole.ToolTipRole:
            return root.canonical_path if index.column() == 0 else root.status_message
        if role == Qt.ItemDataRole.CheckStateRole and index.column() in {1, 2}:
            checked = root.enabled if index.column() == 1 else root.recursive
            return Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        status = f"{'可用' if root.available else '不可用'} · {root.indexed_count} 个"
        if root.last_scan_at:
            status += f" · {root.last_scan_at}"
        if root.status_message:
            status += f" · {root.status_message}"
        return (root.display_name, "", "", status)[index.column()]

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and index.column() in {1, 2}:
            flags |= Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if (
            not index.isValid() or role != Qt.ItemDataRole.CheckStateRole
            or index.column() not in {1, 2}
        ):
            return False
        root = self.rows[index.row()]
        checked = value == Qt.CheckState.Checked.value or value == Qt.CheckState.Checked
        try:
            if self._update_callback:
                self._update_callback(
                    root.root_id,
                    enabled=checked if index.column() == 1 else None,
                    recursive=checked if index.column() == 2 else None,
                )
        except Exception:
            return False
        if index.column() == 1:
            root.enabled = checked
        else:
            root.recursive = checked
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        return True

    def set_records(self, roots: list[ScanRoot]) -> None:
        self.beginResetModel()
        self.rows = list(roots)
        self.endResetModel()

    def append_root(self, root: ScanRoot) -> int:
        row = len(self.rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self.rows.append(root)
        self.endInsertRows()
        return row

    def remove_root(self, root_id: int) -> bool:
        row = self.row_for_id(root_id)
        if row < 0:
            return False
        self.beginRemoveRows(QModelIndex(), row, row)
        del self.rows[row]
        self.endRemoveRows()
        return True

    def row_for_id(self, root_id: int | None) -> int:
        return next(
            (i for i, root in enumerate(self.rows) if root.root_id == root_id),
            -1,
        )


class ReferenceTableModel(QAbstractTableModel):
    HEADERS = ("脚本", "角色", "状态")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[ScriptReference] = []

    def set_records(self, values: list[ScriptReference]) -> None:
        self.beginResetModel()
        self.rows = list(values)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section] if 0 <= section < len(self.HEADERS) else None
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        reference = self.rows[index.row()]
        if role == REFERENCE_ROLE:
            return reference
        if role == Qt.ItemDataRole.UserRole:
            return reference.script_path
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(
                value for value in (
                    reference.script_path, reference.model_path,
                    reference.reason, *reference.candidates,
                ) if value
            )
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return (
            reference.script_name,
            ROLE_LABELS.get(reference.role, reference.role.value),
            REFERENCE_STATE_LABELS.get(reference.state, reference.state.value),
        )[index.column()]


class ComponentTableModel(QAbstractTableModel):
    COMPONENT_HEADERS = ("组件", "角色", "引用脚本", "状态", "可信度")
    PRIMARY_HEADERS = ("主模型", "角色", "引用脚本", "状态", "可信度")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[AssociatedComponent] = []
        self.subject_is_auxiliary = False

    @property
    def headers(self) -> tuple[str, ...]:
        return (
            self.PRIMARY_HEADERS
            if self.subject_is_auxiliary else self.COMPONENT_HEADERS
        )

    def set_records(
        self, values: list[AssociatedComponent], *,
        subject_is_auxiliary: bool = False,
    ) -> None:
        self.beginResetModel()
        self.rows = list(values)
        self.subject_is_auxiliary = subject_is_auxiliary
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section] if 0 <= section < len(self.headers) else None
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        item = self.rows[index.row()]
        if role == COMPONENT_ROLE:
            return item
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join((
                item.canonical_path, item.script_path,
                f"角色来源：{item.confidence}",
            ))
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return (
            item.filename,
            MODEL_FILE_ROLE_LABELS[item.role],
            item.script_name,
            item.existence_state,
            item.confidence,
        )[index.column()]


def format_file_size(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{value} B"
        amount /= 1024
    return str(value)


class ModelTableModel(QAbstractTableModel):
    HEADERS = ("模型", "系列", "模型规模", "量化", "文件大小", "引用脚本", "状态")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.rows: list[ModelRecord] = []
        self._loader: Callable[[int], list[ModelRecord]] | None = None
        self._page_size = 250
        self._more_available = False

    def set_records(
        self, records: list[ModelRecord], *,
        loader: Callable[[int], list[ModelRecord]] | None = None,
        page_size: int = 250,
    ) -> None:
        self.beginResetModel()
        self.rows = records
        self._loader = loader
        self._page_size = page_size
        self._more_available = loader is not None and len(records) >= page_size
        self.endResetModel()

    def canFetchMore(self, parent=QModelIndex()) -> bool:
        return not parent.isValid() and self._more_available

    def fetchMore(self, parent=QModelIndex()) -> None:
        if parent.isValid() or not self._more_available or self._loader is None:
            return
        values = self._loader(len(self.rows))
        if values:
            first = len(self.rows)
            self.beginInsertRows(QModelIndex(), first, first + len(values) - 1)
            self.rows.extend(values)
            self.endInsertRows()
        self._more_available = len(values) >= self._page_size

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self.HEADERS)
        ):
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        record = self.rows[index.row()]
        if role == MODEL_ROLE:
            return record
        if role == MODEL_FILE_ROLE_ROLE:
            return record.model_role.value
        if role == Qt.ItemDataRole.UserRole:
            return record.model_id
        if role == Qt.ItemDataRole.AccessibleTextRole and index.column() == 0:
            return (
                f"{MODEL_FILE_ROLE_LABELS[record.model_role]}，"
                f"{record.name or record.filename}"
            )
        if (
            role == Qt.ItemDataRole.ForegroundRole
            and index.column() == 0
            and record.model_role != ModelFileRole.PRIMARY
        ):
            return QBrush(
                model_role_color(QApplication.instance(), record.model_role.value)
            )
        if role == Qt.ItemDataRole.ToolTipRole:
            try:
                provenance = __import__("json").loads(record.provenance_json)
            except ValueError:
                provenance = {}
            field = {
                1: "display.architecture", 2: "display.nominal_size",
                3: "display.quantization",
            }.get(index.column())
            detail = str(provenance.get(field, "")) if field else ""
            return "\n".join(
                value for value in (
                    record.canonical_path,
                    f"角色：{MODEL_FILE_ROLE_LABELS[record.model_role]}（{record.role_provenance}）",
                    f"系列：{record.architecture_family}（{record.architecture_provenance}）",
                    f"标称规模：{record.nominal_size or '未知'}（{record.size_provenance}）",
                    (
                        f"实算总参数：{parameter_scale(record.parameter_count)}"
                        if record.parameter_count is not None else ""
                    ),
                    (
                        f"量化：{record.quantization}；来源："
                        + ("general.file_type" if record.file_type is not None else "文件名推断")
                        if record.quantization else ""
                    ),
                    detail, record.parse_error
                ) if value
            )
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return (
                Qt.AlignmentFlag.AlignVCenter
                | (
                    Qt.AlignmentFlag.AlignLeft
                    if index.column() in {0, 1}
                    else Qt.AlignmentFlag.AlignHCenter
                )
            )
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        status = {
            ModelStatus.AVAILABLE: "可用",
            ModelStatus.MISSING: "文件缺失",
            ModelStatus.ERROR: PARSE_STATUS_LABELS.get(
                record.parse_status, "读取错误"
            ),
        }[record.status]
        if record.status == ModelStatus.AVAILABLE and record.parse_status != ParseStatus.PARSED:
            status += f" · {PARSE_STATUS_LABELS.get(record.parse_status, '元数据受限')}"
        if record.possible_duplicate:
            status += " · 疑似重复"
        marker = MODEL_FILE_ROLE_MARKERS.get(record.model_role, "")
        model_name = record.name or record.filename
        values = (
            f"{marker} {model_name}" if marker else model_name,
            record.architecture_family or "未知",
            record.nominal_size or "未知",
            record.quantization or "未知",
            format_file_size(record.size),
            (
                f"主 {record.primary_reference_count} / 辅 "
                f"{record.auxiliary_reference_count}"
            ),
            status,
        )
        return values[index.column()]

    def record_at(self, row: int) -> ModelRecord | None:
        return self.rows[row] if 0 <= row < len(self.rows) else None


class ModelFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.text = ""
        self.architecture = ""
        self.quantization = ""
        self.reference_filter = "all"
        self.status_filter = "all"
        self.tag = ""
        self.show_auxiliary = False
        self.setDynamicSortFilter(True)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_filters(
        self, *, text: str | None = None, architecture: str | None = None,
        quantization: str | None = None, reference_filter: str | None = None,
        status_filter: str | None = None, tag: str | None = None,
        show_auxiliary: bool | None = None,
    ) -> None:
        if text is not None:
            self.text = text.strip().casefold()
        if architecture is not None:
            self.architecture = architecture.strip().casefold()
        if quantization is not None:
            self.quantization = quantization.strip().casefold()
        if reference_filter is not None:
            self.reference_filter = reference_filter
        if status_filter is not None:
            self.status_filter = status_filter
        if tag is not None:
            self.tag = tag.strip().casefold()
        if show_auxiliary is not None:
            self.show_auxiliary = show_auxiliary
        self.beginFilterChange()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        record = model.data(index, MODEL_ROLE)
        if not isinstance(record, ModelRecord):
            return False
        if not self.show_auxiliary and is_hidden_auxiliary(record.model_role):
            return False
        searchable = "\n".join(
            (
                record.name, record.filename, record.canonical_path,
                record.architecture, record.architecture_family,
                record.quantization, record.nominal_size,
                " ".join(record.tags), record.notes,
            )
        ).casefold()
        if self.text and self.text not in searchable:
            return False
        if (
            self.architecture
            and record.architecture_family.casefold() != self.architecture
        ):
            return False
        if self.quantization and record.quantization.casefold() != self.quantization:
            return False
        if self.reference_filter == "referenced" and record.reference_count == 0:
            return False
        if self.reference_filter == "unreferenced" and record.reference_count != 0:
            return False
        if self.status_filter != "all" and record.status.value != self.status_filter:
            return False
        if self.tag and self.tag not in {item.casefold() for item in record.tags}:
            return False
        return True
