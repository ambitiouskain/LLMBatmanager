from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableView,
    QVBoxLayout, QWidget,
)

from ...domain.models import ScriptRecord
from .models import ModelRecord, ScanSummary, ScanSuppressedError
from .presentation import is_hidden_auxiliary
from .qt_models import (
    MODEL_ROLE, REFERENCE_ROLE, ComponentTableModel, MODEL_FILE_ROLE_LABELS,
    ModelFilterProxy, ModelTableModel, PARSE_STATUS_LABELS,
    ReferenceTableModel, RootTableModel,
)
from .service import ModelLibraryService


class _ScanWorker(QObject):
    finished = Signal(str, object)
    failed = Signal(str, str)
    batch = Signal(str, int)

    def __init__(
        self, service: ModelLibraryService, job_id: str,
        root_ids: list[int] | None, model_id: int | None = None,
    ) -> None:
        super().__init__()
        self.service = service
        self.job_id = job_id
        self.root_ids = root_ids
        self.model_id = model_id

    @Slot()
    def run(self) -> None:
        try:
            if self.model_id is not None:
                result = self.service.run_rescan_one(
                    self.job_id, self.model_id
                )
            else:
                result = self.service.run_scan(
                    self.job_id, self.root_ids,
                    batch_callback=lambda job, values: self.batch.emit(
                        job, len(values)
                    ),
                )
        except Exception as error:
            self.failed.emit(self.job_id, str(error))
        else:
            self.finished.emit(self.job_id, result)


class ModelLibraryPage(QWidget):
    navigateToScript = Signal(str)
    statusMessage = Signal(str)
    columnWidthsChanged = Signal(dict)

    def __init__(
        self, data_dir: Path, records: list[ScriptRecord],
        *, allow_running_scan: bool = False,
        service_active: bool = False,
        service: ModelLibraryService | None = None,
        column_widths: dict[str, list[int]] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.extension_service = service or ModelLibraryService(
            data_dir, allow_running_scan=allow_running_scan
        )
        self._pending_reference_records: list[ScriptRecord] | None = None
        self.extension_service.set_service_active(service_active)
        if service_active:
            self._pending_reference_records = records
        else:
            self.extension_service.sync_references(records)
        self._thread: QThread | None = None
        self._worker: _ScanWorker | None = None
        self._expected_job: str | None = None
        self._scan_processed = 0
        self._selected: ModelRecord | None = None
        self._saved_column_widths = column_widths or {}
        self._column_defaults: dict[str, list[int]] = {}
        self._build_ui()
        self.refresh_roots()
        self.refresh_models()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.performance_notice = QLabel(
            "模型库仅在手动扫描时读取已配置目录；不会代理请求、加载模型或执行完整文件哈希。"
        )
        self.performance_notice.setObjectName("Muted")
        self.performance_notice.setWordWrap(True)
        outer.addWidget(self.performance_notice)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, 1)

        roots = QFrame()
        roots.setObjectName("Card")
        root_layout = QVBoxLayout(roots)
        root_layout.addWidget(QLabel("扫描目录"))
        self.root_model = RootTableModel(
            self.extension_service.store.update_root, self
        )
        self.root_table = QTableView()
        self.root_table.setModel(self.root_model)
        self.root_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.root_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._configure_table(
            self.root_table, "scan_roots", [220, 68, 68, 220]
        )
        root_layout.addWidget(self.root_table, 1)
        self.add_root_button = QPushButton("添加扫描目录")
        self.add_root_button.clicked.connect(self.add_root)
        self.remove_root_button = QPushButton("移除扫描目录")
        self.remove_root_button.clicked.connect(self.remove_selected_root)
        self.scan_selected_button = QPushButton("扫描所选目录")
        self.scan_selected_button.clicked.connect(self.scan_selected)
        self.scan_all_button = QPushButton("扫描全部目录")
        self.scan_all_button.clicked.connect(self.scan_all)
        self.cancel_button = QPushButton("取消扫描")
        self.cancel_button.clicked.connect(self.cancel_scan)
        for button in (
            self.add_root_button, self.remove_root_button,
            self.scan_selected_button, self.scan_all_button,
            self.cancel_button,
        ):
            root_layout.addWidget(button)
        self._set_scan_action_state("idle")
        self.scan_status = QLabel("未扫描")
        self.scan_status.setWordWrap(True)
        root_layout.addWidget(self.scan_status)
        root_layout.addWidget(QLabel("缺失或无法确认的脚本引用"))
        self.problem_references = QListWidget()
        self.problem_references.setMaximumHeight(150)
        self.problem_references.itemDoubleClicked.connect(
            lambda item: self.navigateToScript.emit(
                item.data(Qt.ItemDataRole.UserRole)
            )
        )
        root_layout.addWidget(self.problem_references)
        splitter.addWidget(roots)

        center = QFrame()
        center.setObjectName("Card")
        center_layout = QVBoxLayout(center)
        filter_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索模型、路径、架构、标签或备注")
        self.search.setClearButtonEnabled(True)
        self.architecture_filter = QComboBox()
        self.architecture_filter.addItem("全部架构", "")
        self.quantization_filter = QComboBox()
        self.quantization_filter.addItem("全部量化", "")
        self.reference_filter = QComboBox()
        self.reference_filter.addItem("全部引用状态", "all")
        self.reference_filter.addItem("已引用", "referenced")
        self.reference_filter.addItem("未引用", "unreferenced")
        self.status_filter = QComboBox()
        self.status_filter.addItem("全部文件状态", "all")
        self.status_filter.addItem("可用", "available")
        self.status_filter.addItem("缺失", "missing")
        self.status_filter.addItem("错误", "error")
        self.tag_filter = QLineEdit()
        self.tag_filter.setPlaceholderText("标签")
        self.show_auxiliary = QCheckBox("显示附属 GGUF")
        self.show_auxiliary.setToolTip(
            "默认只显示主模型、嵌入/重排模型和角色未知的独立文件"
        )
        for widget in (
            self.search, self.architecture_filter, self.quantization_filter,
            self.reference_filter, self.status_filter, self.tag_filter,
            self.show_auxiliary,
        ):
            filter_row.addWidget(widget)
        center_layout.addLayout(filter_row)
        self.model_model = ModelTableModel(self)
        self.proxy_model = ModelFilterProxy(self)
        self.proxy_model.setSourceModel(self.model_model)
        self.model_table = QTableView()
        self.model_table.setModel(self.proxy_model)
        self.model_table.setSortingEnabled(True)
        self.model_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.model_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._configure_table(
            self.model_table, "models",
            [240, 140, 110, 115, 105, 115, 170],
        )
        self.model_table.selectionModel().selectionChanged.connect(
            self._selection_changed
        )
        center_layout.addWidget(self.model_table, 1)
        self.search.textChanged.connect(self._apply_filters)
        self.architecture_filter.currentIndexChanged.connect(self._apply_filters)
        self.quantization_filter.currentIndexChanged.connect(self._apply_filters)
        self.reference_filter.currentIndexChanged.connect(self._apply_filters)
        self.status_filter.currentIndexChanged.connect(self._apply_filters)
        self.tag_filter.textChanged.connect(self._apply_filters)
        self.show_auxiliary.toggled.connect(self._apply_filters)
        self.auxiliary_hint = QLabel()
        self.auxiliary_hint.setObjectName("Muted")
        center_layout.addWidget(self.auxiliary_hint)
        splitter.addWidget(center)

        details = QFrame()
        details.setObjectName("Card")
        detail_layout = QVBoxLayout(details)
        detail_layout.addWidget(QLabel("模型详情"))
        self.detail_text = QPlainTextEdit()
        self.detail_text.setReadOnly(True)
        detail_layout.addWidget(self.detail_text, 2)
        detail_layout.addWidget(QLabel("引用脚本"))
        self.reference_model = ReferenceTableModel(self)
        self.references = QTableView()
        self.references.setModel(self.reference_model)
        self.references.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.references.doubleClicked.connect(
            lambda index: self.navigateToScript.emit(
                str(self.reference_model.data(index, Qt.ItemDataRole.UserRole))
            )
        )
        self._configure_table(
            self.references, "references", [190, 110, 110]
        )
        detail_layout.addWidget(self.references, 1)
        self.component_title = QLabel("关联组件（0）")
        detail_layout.addWidget(self.component_title)
        self.component_model = ComponentTableModel(self)
        self.components = QTableView()
        self.components.setModel(self.component_model)
        self.components.setSelectionBehavior(
            QTableView.SelectionBehavior.SelectRows
        )
        self._configure_table(
            self.components, "components", [170, 120, 150, 90, 120]
        )
        detail_layout.addWidget(self.components, 1)
        self.component_empty = QLabel("无关联组件")
        self.component_empty.setObjectName("Muted")
        self.component_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail_layout.addWidget(self.component_empty)
        self.components.hide()
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("标签，以逗号分隔")
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("本地备注")
        self.notes.setMaximumBlockCount(500)
        detail_layout.addWidget(self.tags)
        detail_layout.addWidget(self.notes, 1)
        save = QPushButton("保存标签和备注")
        save.clicked.connect(self.save_metadata)
        detail_layout.addWidget(save)
        actions = QHBoxLayout()
        for label, callback in (
            ("打开所在目录", self.open_folder),
            ("复制完整路径", self.copy_path),
            ("复制文件名", self.copy_filename),
            ("重新读取所选元数据", self.rescan_selected_model),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            actions.addWidget(button)
        detail_layout.addLayout(actions)
        splitter.addWidget(details)
        splitter.setSizes([260, 820, 400])

    def _configure_table(
        self, table: QTableView, key: str, defaults: list[int]
    ) -> None:
        self._column_defaults[key] = defaults
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(55)
        for column in range(table.model().columnCount()):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Interactive
            )
        values = self._saved_column_widths.get(key, [])
        if not isinstance(values, list) or len(values) != len(defaults):
            values = defaults
        for column, width in enumerate(values):
            if isinstance(width, int) and 55 <= width <= 3000:
                table.setColumnWidth(column, width)
            else:
                table.setColumnWidth(column, defaults[column])
        header.sectionDoubleClicked.connect(table.resizeColumnToContents)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda position, t=table, k=key: self._column_menu(t, k, position)
        )
        table.setHorizontalScrollMode(
            QTableView.ScrollMode.ScrollPerPixel
        )

    def _column_menu(
        self, table: QTableView, key: str, position
    ) -> None:
        menu = QMenu(table)
        reset = menu.addAction("恢复默认列宽")
        if menu.exec(table.horizontalHeader().mapToGlobal(position)) == reset:
            self._reset_column_widths(table, key)

    def _reset_column_widths(self, table: QTableView, key: str) -> None:
        for column, width in enumerate(self._column_defaults[key]):
            table.setColumnWidth(column, width)

    def column_widths(self) -> dict[str, list[int]]:
        return {
            key: [
                table.columnWidth(index)
                for index in range(table.model().columnCount())
            ]
            for key, table in (
                ("scan_roots", self.root_table),
                ("models", self.model_table),
                ("references", self.references),
                ("components", self.components),
            )
        }

    def refresh_roots(self) -> None:
        self.root_model.set_records(self.extension_service.roots())

    def refresh_models(self) -> None:
        selected_path = (
            self._selected.canonical_path if self._selected else ""
        )
        page_size = 250
        records = self.extension_service.models(limit=page_size)
        self.model_model.set_records(
            records,
            page_size=page_size,
            loader=lambda offset: self.extension_service.models(
                limit=page_size, offset=offset
            ),
        )
        self._populate_filter_values(records)
        hidden = self.extension_service.store.auxiliary_count()
        self.auxiliary_hint.setText(
            (
                f"当前显示附属 GGUF；共 {hidden} 个"
                if self.show_auxiliary.isChecked()
                else f"默认已隐藏 {hidden} 个附属 GGUF"
            )
        )
        self._apply_filters()
        self.problem_references.clear()
        labels = {
            "missing": "文件缺失",
            "multiple": "多个候选模型",
            "ambiguous": "歧义候选",
            "dynamic": "动态，无法确认",
            "unparsed": "未解析",
        }
        for reference in self.extension_service.store.problem_references():
            self.problem_references.addItem(
                f"{reference.script_name} · {labels.get(reference.state.value, reference.state.value)}"
            )
            item = self.problem_references.item(
                self.problem_references.count() - 1
            )
            item.setToolTip(
                "\n".join(
                    value for value in (
                        reference.model_path,
                        reference.reason,
                        *reference.candidates,
                    ) if value
                )
            )
            item.setData(Qt.ItemDataRole.UserRole, reference.script_path)
        if selected_path:
            for row, record in enumerate(self.model_model.rows):
                if record.canonical_path.casefold() == selected_path.casefold():
                    proxy = self.proxy_model.mapFromSource(
                        self.model_model.index(row, 0)
                    )
                    if proxy.isValid():
                        self.model_table.selectRow(proxy.row())
                        self.model_table.scrollTo(proxy)
                    break

    def _populate_filter_values(self, records: list[ModelRecord]) -> None:
        current_arch = self.architecture_filter.currentData()
        current_quant = self.quantization_filter.currentData()
        architectures = sorted(
            {
                r.architecture_family for r in records
                if r.architecture_family
            },
            key=str.casefold,
        )
        quantizations = sorted({r.quantization for r in records if r.quantization}, key=str.casefold)
        self.architecture_filter.blockSignals(True)
        self.quantization_filter.blockSignals(True)
        self.architecture_filter.clear()
        self.architecture_filter.addItem("全部系列", "")
        for value in architectures:
            self.architecture_filter.addItem(value, value)
        self.quantization_filter.clear()
        self.quantization_filter.addItem("全部量化", "")
        for value in quantizations:
            self.quantization_filter.addItem(value, value)
        self.architecture_filter.setCurrentIndex(
            max(0, self.architecture_filter.findData(current_arch))
        )
        self.quantization_filter.setCurrentIndex(
            max(0, self.quantization_filter.findData(current_quant))
        )
        self.architecture_filter.blockSignals(False)
        self.quantization_filter.blockSignals(False)

    def _apply_filters(self, *_args) -> None:
        self.proxy_model.set_filters(
            text=self.search.text(),
            architecture=self.architecture_filter.currentData() or "",
            quantization=self.quantization_filter.currentData() or "",
            reference_filter=self.reference_filter.currentData(),
            status_filter=self.status_filter.currentData(),
            tag=self.tag_filter.text(),
            show_auxiliary=self.show_auxiliary.isChecked(),
        )

    def add_root(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "选择 GGUF 扫描目录")
        if not value:
            return
        try:
            root, created = self.extension_service.add_root_with_status(
                Path(value)
            )
        except Exception as error:
            self._show_error(str(error))
            return
        row = self.root_model.row_for_id(root.root_id)
        if created:
            row = self.root_model.append_root(root)
            message = "扫描目录已添加"
        else:
            if row < 0:
                row = self.root_model.append_root(root)
            message = "该扫描目录已经存在"
        index = self.root_model.index(row, 0)
        self.root_table.selectRow(row)
        self.root_table.scrollTo(index, QTableView.ScrollHint.PositionAtCenter)
        self.scan_status.setText(message)
        self.statusMessage.emit(message)

    def _selected_root_id(self) -> int | None:
        rows = self.root_table.selectionModel().selectedRows()
        return self.root_model.data(
            self.root_model.index(rows[0].row(), 0),
            Qt.ItemDataRole.UserRole,
        ) if rows else None

    def remove_selected_root(self) -> None:
        root_id = self._selected_root_id()
        if root_id is None:
            return
        try:
            self.extension_service.remove_root(root_id)
        except Exception as error:
            self._show_error(str(error))
            return
        self.root_model.remove_root(root_id)
        self.refresh_models()
        self.scan_status.setText("扫描目录已移除")
        self.statusMessage.emit("扫描目录已移除")

    def scan_selected(self) -> None:
        root_id = self._selected_root_id()
        if root_id is not None:
            self._start_scan([root_id])

    def scan_all(self) -> None:
        self._start_scan(None)

    def _start_scan(
        self, root_ids: list[int] | None, model_id: int | None = None
    ) -> None:
        if self._thread is not None:
            return
        if (
            self.extension_service._service_active
            and self.extension_service.allow_running_scan
        ):
            answer = QMessageBox.warning(
                self, "模型运行期间扫描",
                "磁盘活动可能影响模型加载或推理。仅在明确需要时继续。",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Ok:
                return
        try:
            job_id = self.extension_service.begin_scan(root_ids)
        except ScanSuppressedError as error:
            self.scan_status.setText(str(error))
            return
        except Exception as error:
            self._show_error(str(error))
            return
        self._expected_job = job_id
        self._scan_processed = 0
        stale = (
            self.extension_service.stale_cache_count(root_ids)
            if model_id is None else 0
        )
        self.scan_status.setText(
            "旧版元数据缓存，将在本次手动扫描中重新读取"
            if stale else "正在扫描……"
        )
        self._set_scan_action_state("scanning")
        thread = QThread(self)
        worker = _ScanWorker(
            self.extension_service, job_id, root_ids, model_id
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.batch.connect(self._scan_batch)
        worker.finished.connect(self._scan_finished)
        worker.failed.connect(self._scan_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._thread_finished)
        self._thread, self._worker = thread, worker
        thread.start(QThread.Priority.LowPriority)

    @Slot(str, int)
    def _scan_batch(self, job_id: str, count: int) -> None:
        if job_id == self._expected_job:
            self._scan_processed += count
            self.scan_status.setText(
                f"正在扫描：已处理 {self._scan_processed} 个文件"
            )

    @Slot(str, object)
    def _scan_finished(self, job_id: str, result: object) -> None:
        if job_id != self._expected_job:
            return
        if isinstance(result, ScanSummary):
            self.scan_status.setText(
                "扫描已取消" if result.cancelled else
                f"扫描完成：新增/更新 {result.parsed}，未变化 {result.unchanged}，"
                f"错误 {result.failed}，缺失 {result.missing}"
            )
        else:
            self.scan_status.setText("所选模型元数据已重新读取")
        self.refresh_roots()
        self.refresh_models()

    @Slot(str, str)
    def _scan_failed(self, job_id: str, error: str) -> None:
        if job_id == self._expected_job:
            self.scan_status.setText(f"扫描失败：{error}")

    @Slot()
    def _thread_finished(self) -> None:
        if self._worker:
            self._worker.deleteLater()
        if self._thread:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        self._set_scan_action_state("idle")

    def cancel_scan(self) -> None:
        if self._thread is None:
            return
        self.extension_service.cancel_scan()
        self._set_scan_action_state("stopping")
        self.scan_status.setText("正在安全取消；当前文件关闭后停止……")

    def set_model_service_active(self, active: bool) -> None:
        cancelled = self.extension_service.set_service_active(active)
        if cancelled:
            self._set_scan_action_state("stopping")
            self.scan_status.setText("为避免影响模型性能，模型运行期间已暂停扫描")
        if not active and self._pending_reference_records is not None:
            pending = self._pending_reference_records
            self._pending_reference_records = None
            self.extension_service.sync_references(pending)
            self.refresh_models()

    def sync_references(self, records: list[ScriptRecord]) -> None:
        if self.extension_service._service_active:
            self._pending_reference_records = records
            return
        self.extension_service.sync_references(records)
        self.refresh_models()

    def _selection_changed(self, *_args) -> None:
        rows = self.model_table.selectionModel().selectedRows()
        if not rows:
            self._selected = None
            self.detail_text.clear()
            self.reference_model.set_records([])
            self._update_associated_section(None)
            return
        source = self.proxy_model.mapToSource(rows[0])
        record = self.model_model.data(source, MODEL_ROLE)
        if not isinstance(record, ModelRecord):
            return
        self._selected = record
        metadata = json.loads(record.metadata_json)
        provenance = json.loads(record.provenance_json)
        self.detail_text.setPlainText(
            f"名称：{record.name or record.filename}\n"
            f"general.name：{metadata.get('general.name', '未提供')}\n"
            f"文件名：{record.filename}\n"
            f"路径：{record.canonical_path}\n"
            f"角色：{MODEL_FILE_ROLE_LABELS[record.model_role]}（{record.role_provenance}）\n"
            f"系列：{record.architecture_family}（{record.architecture_provenance}）\n"
            f"原始架构标识：{record.architecture or '未提供'}\n"
            f"标称规模：{record.nominal_size or '未知'}（{record.size_provenance}）\n"
            f"实算总参数：{record.parameter_count if record.parameter_count is not None else '未知'}\n"
            f"激活参数：{record.active_parameter_count if record.active_parameter_count is not None else '未知'}\n"
            f"量化：{record.quantization or '未知'}\n"
            f"原始 file_type：{record.file_type if record.file_type is not None else '未提供'}\n"
            f"quantization_version：{metadata.get('general.quantization_version', '未提供')}\n"
            "说明：general.file_type 表示文件中占主导的 tensor/文件类型；"
            "它不同于 quantization_version。\n"
            f"上下文：{record.context_length if record.context_length is not None else '未知'}\n"
            f"Tensor：{record.tensor_count if record.tensor_count is not None else '未知'}\n"
            f"解析状态：{PARSE_STATUS_LABELS.get(record.parse_status, '未知')}\n"
            f"缓存读取器版本：{record.metadata_reader_version}\n"
            f"错误：{record.parse_error or '无'}\n\n"
            f"来源：\n{json.dumps(provenance, ensure_ascii=False, indent=2)}\n\n"
            f"保留元数据：\n{json.dumps(metadata, ensure_ascii=False, indent=2)}"
        )
        self.tags.setText(", ".join(record.tags))
        self.notes.setPlainText(record.notes)
        self.reference_model.set_records(
            self.extension_service.store.references_for_model(
                record.canonical_path
            )
        )
        self._update_associated_section(record)

    def _update_associated_section(
        self, record: ModelRecord | None
    ) -> None:
        auxiliary = bool(
            record and is_hidden_auxiliary(record.model_role)
        )
        if record is None:
            values = []
        elif auxiliary:
            values = self.extension_service.store.primaries_for_component(
                record.canonical_path
            )
        else:
            values = self.extension_service.store.components_for_primary(
                record.canonical_path
            )
        self.component_model.set_records(
            values, subject_is_auxiliary=auxiliary
        )
        subject = "关联主模型" if auxiliary else "关联组件"
        self.component_title.setText(f"{subject}（{len(values)}）")
        self.component_empty.setText(
            "无关联主模型" if auxiliary else "无关联组件"
        )
        self.components.setVisible(bool(values))
        self.component_empty.setVisible(not values)

    def _set_scan_action_state(self, state: str) -> None:
        scanning = state in {"scanning", "stopping"}
        stopping = state == "stopping"
        self.cancel_button.setText(
            "正在停止……" if stopping
            else "停止扫描" if scanning
            else "取消扫描"
        )
        self.cancel_button.setEnabled(state == "scanning")
        self.cancel_button.setProperty(
            "semanticRole", "danger" if scanning else "neutral"
        )
        self.cancel_button.style().unpolish(self.cancel_button)
        self.cancel_button.style().polish(self.cancel_button)
        for button in (
            self.add_root_button, self.remove_root_button,
            self.scan_selected_button, self.scan_all_button,
        ):
            button.setEnabled(not scanning)

    def save_metadata(self) -> None:
        if not self._selected or self._selected.model_id is None:
            return
        tags = [item.strip() for item in self.tags.text().split(",")]
        self.extension_service.save_user_metadata(
            self._selected.model_id, tags, self.notes.toPlainText()
        )
        self.refresh_models()
        self.statusMessage.emit("模型标签和备注已保存")

    def open_folder(self) -> None:
        if self._selected:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._selected.path.parent)))

    def copy_path(self) -> None:
        if self._selected:
            QApplication.clipboard().setText(self._selected.canonical_path)
            self.statusMessage.emit("已复制完整模型路径")

    def copy_filename(self) -> None:
        if self._selected:
            QApplication.clipboard().setText(self._selected.filename)
            self.statusMessage.emit("已复制模型文件名")

    def rescan_selected_model(self) -> None:
        if not self._selected or self._selected.model_id is None:
            return
        self._start_scan(None, self._selected.model_id)

    def _show_error(self, message: str) -> None:
        self.scan_status.setText(message)
        self.statusMessage.emit(message)

    def shutdown(self, wait_ms: int = 5000) -> None:
        self.columnWidthsChanged.emit(self.column_widths())
        self._expected_job = None
        self.extension_service.shutdown()
        if self._thread is not None:
            self._thread.quit()
            if not self._thread.wait(wait_ms):
                # Never destroy a live QThread or abandon an open model file.
                # The reader is bounded, so shutdown waits only for the current
                # safe file boundary and never force-terminates the worker.
                self._thread.wait()
