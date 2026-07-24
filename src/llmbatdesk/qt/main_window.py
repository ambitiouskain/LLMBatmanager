from __future__ import annotations

import base64
from pathlib import Path

from PySide6.QtCore import QItemSelection, QModelIndex, QThreadPool, QTimer, Qt, QUrl
from PySide6.QtGui import (
    QAction, QCloseEvent, QDesktopServices, QKeySequence, QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStackedWidget, QStyle, QTabWidget, QTableView, QToolBar, QVBoxLayout, QWidget,
)

from ..domain.models import Backend, ManagedLaunch, RuntimeState, ScriptRecord
from ..parsing.parser import runtime_api_address
from ..runtime.logging import redact
from ..runtime.ports import ConflictDecision, PortConflictError
from ..services import ApplicationService, LaunchBlockedError, LaunchInProgressError
from .dialogs import (
    AlternatePortDialog, ConflictDialog, DeleteUserDataDialog, MetadataDialog,
    SettingsDialog, TextDialog, TrustDialog,
)
from .help_content import FAQ, SECURITY_GUIDE, USAGE_GUIDE, about_text
from .models import LaunchTableModel, runtime_display_state
from .tasks import Worker
from .terminal_log import LogSeverity, TerminalLogView
from .theme import apply_theme
from .widgets import CopyField, EmptyState


SCRIPT_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class MainWindow(QMainWindow):
    def __init__(
        self,
        service: ApplicationService | None = None,
        *,
        dpi_diagnostics: str = "",
        auto_scan: bool = True,
    ) -> None:
        super().__init__()
        self.service = service or ApplicationService()
        self.dpi_diagnostics = dpi_diagnostics
        self.records: list[ScriptRecord] = []
        self.selected_record: ScriptRecord | None = None
        self._workers: set[Worker] = set()
        self._start_in_progress = False
        self._runtime_refresh_in_progress = False
        self._log_refresh_in_progress = False
        self._api_polling: set[str] = set()
        self.settings_dialog: SettingsDialog | None = None
        self._build_ui()
        self._restore_window_state()
        self._update_empty_details()
        self._update_actions()
        self.runtime_timer = QTimer(self)
        self.runtime_timer.timeout.connect(self.refresh_runtime_async)
        self.runtime_timer.start(3000)
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.refresh_log_async)
        self.log_timer.start(1500)
        if auto_scan and (
            self.service.settings.roots or self.service.settings.individual_scripts
        ):
            QTimer.singleShot(0, self.rescan_async)
        else:
            self.bind_records(list(self.service.records.values()))
        QTimer.singleShot(0, self.cleanup_storage_async)

    # ---- Construction -------------------------------------------------

    def _build_ui(self) -> None:
        self.setWindowTitle("LLMBatDesk — 本地模型启动脚本管理器")
        self.setMinimumSize(1080, 660)
        self.resize(1480, 880)
        self.setStatusBar(self.statusBar())
        self.statusBar().showMessage("就绪")
        self._build_menus()

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 8)
        outer.setSpacing(8)
        header = QHBoxLayout()
        title = QLabel("LLMBatDesk")
        title.setObjectName("Hero")
        header.addWidget(title)
        subtitle = QLabel("脚本是技术配置的唯一事实来源")
        subtitle.setObjectName("Muted")
        header.addWidget(subtitle)
        header.addStretch()
        add_root = QPushButton("添加目录")
        add_root.clicked.connect(self.add_root)
        header.addWidget(add_root)
        add_script = QPushButton("添加脚本")
        add_script.clicked.connect(self.add_script)
        header.addWidget(add_script)
        outer.addLayout(header)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(5)
        self.left_pane = self._build_library_pane()
        self.center_pane = self._build_detail_pane()
        self.right_pane = self._build_runtime_pane()
        self.main_splitter.addWidget(self.left_pane)
        self.main_splitter.addWidget(self.center_pane)
        self.main_splitter.addWidget(self.right_pane)
        self.main_splitter.setStretchFactor(0, 2)
        self.main_splitter.setStretchFactor(1, 5)
        self.main_splitter.setStretchFactor(2, 4)
        self.main_splitter.setSizes([300, 720, 460])
        outer.addWidget(self.main_splitter, 1)
        self.setCentralWidget(central)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        file_menu.addAction("添加扫描目录…", self.add_root)
        file_menu.addAction("添加单个脚本…", self.add_script)
        file_menu.addSeparator()
        file_menu.addAction("设置…", self.show_settings)
        file_menu.addAction("打开日志目录", self.open_log_directory)
        file_menu.addAction("退出", self.close)
        view_menu = self.menuBar().addMenu("视图")
        self.theme_action = view_menu.addAction("切换明暗主题")
        self.theme_action.triggered.connect(self.toggle_theme)
        help_menu = self.menuBar().addMenu("帮助")
        help_actions = (
            ("help_usage", "使用说明", self.show_usage_guide),
            ("help_faq", "常见问题", self.show_faq),
            ("help_security", "安全机制与风险提示", self.show_security),
            ("help_dpi", "DPI 与字体诊断", self.show_dpi_diagnostics),
            ("help_about", "关于 LLMBatDesk", self.show_about),
        )
        for object_name, label, callback in help_actions:
            action = help_menu.addAction(label, callback)
            action.setObjectName(object_name)

    def _card(self) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("Card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        return frame, layout

    def _build_library_pane(self) -> QWidget:
        pane = QFrame()
        pane.setObjectName("Card")
        pane.setMinimumWidth(220)
        pane.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(pane)
        heading = QLabel("脚本库")
        heading.setObjectName("Hero")
        layout.addWidget(heading)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索文件、模型、标签、后端、游戏或备注")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_library)
        layout.addWidget(self.search)
        self.library_model = QStandardItemModel(self)
        self.library_tree = QTableView()  # replaced below to keep type-visible test names stable
        from PySide6.QtWidgets import QTreeView
        tree = QTreeView()
        self.library_tree = tree
        tree.setModel(self.library_model)
        tree.setHeaderHidden(True)
        tree.setUniformRowHeights(True)
        tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tree.setAccessibleName("脚本库")
        tree.selectionModel().selectionChanged.connect(self._library_selection_changed)
        tree.activated.connect(self._library_activated)
        layout.addWidget(tree, 1)
        self.library_count = QLabel("尚未扫描")
        self.library_count.setObjectName("Muted")
        layout.addWidget(self.library_count)
        return pane

    def _build_detail_pane(self) -> QWidget:
        pane = QWidget()
        pane.setMinimumWidth(380)
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        self.detail_stack = QStackedWidget()
        self.detail_empty = EmptyState(
            "请选择脚本",
            "从左侧选择一个脚本，或使用“添加目录 / 添加脚本”。\n"
            "解析详情、可运行性和实时运行状态会分别显示。",
        )
        self.detail_stack.addWidget(self.detail_empty)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        hero, hero_layout = self._card()
        self.display_name_label = QLabel()
        self.display_name_label.setObjectName("Hero")
        hero_layout.addWidget(self.display_name_label)
        self.filename_label = QLabel()
        self.filename_label.setObjectName("Muted")
        hero_layout.addWidget(self.filename_label)
        badges = QHBoxLayout()
        self.parse_badge = QLabel()
        self.runnable_badge = QLabel()
        self.runtime_badge = QLabel()
        for badge in (self.parse_badge, self.runnable_badge, self.runtime_badge):
            badge.setObjectName("Badge")
            badges.addWidget(badge)
        badges.addStretch()
        hero_layout.addLayout(badges)
        content_layout.addWidget(hero)

        self.detail_tabs = QTabWidget()
        self.detail_tabs.currentChanged.connect(self._tab_changed)
        self.overview_tab = self._build_overview_tab()
        self.technical_text = self._readonly_text(wrap=True)
        self.warnings_text = self._readonly_text(wrap=True)
        self.metadata_text = self._readonly_text(wrap=True)
        metadata_page = QWidget()
        metadata_layout = QVBoxLayout(metadata_page)
        metadata_layout.setContentsMargins(0, 0, 0, 0)
        metadata_layout.addWidget(self.metadata_text, 1)
        metadata_button = QPushButton("编辑元数据与笔记")
        metadata_button.clicked.connect(self.edit_metadata)
        metadata_layout.addWidget(metadata_button)
        self.raw_text = self._readonly_text(wrap=False)
        self.evidence_text = self._readonly_text(wrap=True)
        for widget, label in (
            (self.overview_tab, "概览"), (self.technical_text, "技术详情"),
            (self.warnings_text, "警告与限制"), (metadata_page, "元数据与笔记"),
            (self.raw_text, "原始脚本"), (self.evidence_text, "解析证据"),
        ):
            self.detail_tabs.addTab(widget, label)
        content_layout.addWidget(self.detail_tabs, 1)
        self.detail_stack.addWidget(content)
        layout.addWidget(self.detail_stack, 1)
        self.action_bar = self._build_action_bar()
        layout.addWidget(self.action_bar)
        return pane

    def _build_overview_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(4, 8, 4, 8)
        grid.setSpacing(8)
        definitions = (
            ("model", "模型名称", QKeySequence("Ctrl+Shift+M"), ""),
            ("model_path", "模型路径", QKeySequence("Ctrl+Shift+P"), "file"),
            ("executable", "可执行文件", None, "file"),
            ("configured_port", "配置端口", None, ""),
            ("actual_port", "实际运行端口", None, ""),
            ("api", "API 地址", QKeySequence("Ctrl+Shift+A"), ""),
            ("script", "脚本路径", QKeySequence("Ctrl+Shift+S"), "file"),
            ("context", "上下文大小", None, ""),
        )
        self.copy_fields: dict[str, CopyField] = {}
        for index, (key, title, shortcut, path_kind) in enumerate(definitions):
            field = CopyField(title, shortcut)
            field.path_kind = path_kind
            field.copied.connect(lambda value: self.statusBar().showMessage(f"已复制：{value}", 2200))
            field.open_file.connect(lambda path: self._open_path(Path(path)))
            field.open_folder.connect(lambda path: self._open_path(Path(path)))
            self.copy_fields[key] = field
            grid.addWidget(field, index // 2, index % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch((len(definitions) + 1) // 2, 1)
        scroll.setWidget(body)
        return scroll

    def _readonly_text(self, *, wrap: bool) -> QPlainTextEdit:
        editor = QPlainTextEdit()
        editor.setReadOnly(True)
        editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
            if wrap else QPlainTextEdit.LineWrapMode.NoWrap
        )
        return editor

    def _build_action_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Card")
        grid = QGridLayout(bar)
        grid.setContentsMargins(8, 7, 8, 7)
        grid.setSpacing(7)
        specifications = (
            ("start", "启动", QStyle.StandardPixmap.SP_MediaPlay, self.start_requested),
            ("stop", "停止", QStyle.StandardPixmap.SP_MediaStop, self.stop_requested),
            ("restart", "重启", QStyle.StandardPixmap.SP_BrowserReload, self.restart_requested),
            ("api", "重新检查 API", QStyle.StandardPixmap.SP_DialogApplyButton, self.test_api_requested),
            ("copy_api", "复制 API 地址", QStyle.StandardPixmap.SP_DialogSaveButton, self.copy_api),
            ("open_api", "打开 API 地址", QStyle.StandardPixmap.SP_DirLinkIcon, self.open_api_address),
            ("open_script", "打开脚本", QStyle.StandardPixmap.SP_FileIcon, self.open_script),
            ("open_folder", "打开所在文件夹", QStyle.StandardPixmap.SP_DirOpenIcon, self.open_folder),
            ("open_log", "打开最新日志", QStyle.StandardPixmap.SP_FileDialogDetailedView, self.open_latest_log),
            ("rescan", "重新扫描", QStyle.StandardPixmap.SP_BrowserReload, self.rescan_async),
        )
        self.actions: dict[str, QPushButton] = {}
        for index, (key, label, icon, callback) in enumerate(specifications):
            button = QPushButton(self.style().standardIcon(icon), label)
            button.clicked.connect(callback)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            grid.addWidget(button, index // 5, index % 5)
            self.actions[key] = button
        for column in range(5):
            grid.setColumnStretch(column, 1)
        return bar

    def _build_runtime_pane(self) -> QWidget:
        pane = QFrame()
        pane.setObjectName("Card")
        pane.setMinimumWidth(320)
        layout = QVBoxLayout(pane)
        heading = QLabel("运行与日志")
        heading.setObjectName("Hero")
        layout.addWidget(heading)
        self.runtime_tabs = QTabWidget()
        self.runtime_tabs.currentChanged.connect(self._runtime_tab_changed)
        self.active_model = LaunchTableModel(True, self)
        self.history_model = LaunchTableModel(False, self)
        self.active_page, self.active_table, self.active_empty = self._table_page(
            self.active_model,
            "当前没有已验证的模型服务在运行",
            "脚本库与解析结果不会出现在这里。",
        )
        self.history_page, self.history_table, self.history_empty = self._table_page(
            self.history_model,
            "暂无启动历史",
            "失败、停止、过期或被阻止的启动会显示在此处。",
        )
        active_header = self.active_table.horizontalHeader()
        active_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        active_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, self.active_model.columnCount()):
            active_header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.log_page = QWidget()
        log_layout = QVBoxLayout(self.log_page)
        search_row = QHBoxLayout()
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("搜索当前日志")
        self.log_search.returnPressed.connect(self.find_in_log)
        search_row.addWidget(self.log_search)
        find_previous = QPushButton("上一个")
        find_previous.clicked.connect(lambda: self.find_in_log(previous=True))
        search_row.addWidget(find_previous)
        find_next = QPushButton("下一个")
        find_next.clicked.connect(self.find_in_log)
        search_row.addWidget(find_next)
        self.log_case_sensitive = QCheckBox("区分大小写")
        search_row.addWidget(self.log_case_sensitive)
        log_layout.addLayout(search_row)
        options = QHBoxLayout()
        self.log_severity = QComboBox()
        self.log_severity.addItem("全部级别", "all")
        self.log_severity.addItem("仅错误", LogSeverity.ERROR)
        self.log_severity.addItem("仅警告", LogSeverity.WARNING)
        self.log_severity.addItem("仅成功/就绪", LogSeverity.SUCCESS)
        self.log_severity.addItem("仅调试", LogSeverity.DEBUG)
        self.log_severity.addItem("仅普通信息", LogSeverity.INFO)
        self.log_severity.currentIndexChanged.connect(self._log_severity_changed)
        options.addWidget(self.log_severity)
        self.log_auto_scroll = QCheckBox("自动滚动")
        self.log_auto_scroll.setChecked(self.service.settings.log_auto_scroll)
        self.log_auto_scroll.toggled.connect(self._log_auto_scroll_changed)
        options.addWidget(self.log_auto_scroll)
        self.log_pause = QCheckBox("暂停画面更新")
        self.log_pause.toggled.connect(
            lambda value: self.log_text.set_visual_updates_paused(value)
        )
        options.addWidget(self.log_pause)
        smaller = QPushButton("A−")
        smaller.setToolTip("减小运行日志字号（Ctrl+鼠标滚轮向下）")
        smaller.clicked.connect(lambda: self.log_text.change_font_size(-1))
        options.addWidget(smaller)
        larger = QPushButton("A+")
        larger.setToolTip("增大运行日志字号（Ctrl+鼠标滚轮向上）")
        larger.clicked.connect(lambda: self.log_text.change_font_size(1))
        options.addWidget(larger)
        options.addStretch()
        log_layout.addLayout(options)
        self.latest_error_field = CopyField("最新错误")
        self.latest_error_field.copied.connect(
            lambda value: self.statusBar().showMessage(f"已复制：{value}", 2200)
        )
        log_layout.addWidget(self.latest_error_field)
        self.log_text = TerminalLogView()
        self._apply_terminal_font(
            self.service.settings.terminal_font_family,
            self.service.settings.terminal_font_size,
            self.service.settings.terminal_line_spacing_percent,
        )
        self.log_text.set_auto_scroll(self.service.settings.log_auto_scroll)
        log_layout.addWidget(self.log_text, 1)
        log_actions = QHBoxLayout()
        for label, callback in (
            ("跳到最新错误", self.jump_to_latest_error),
            ("清空显示", self.clear_log_view),
            ("打开日志文件", self.open_latest_log),
            ("复制选中内容", self.log_text.copy),
            ("复制完整日志", self.log_text.copy_complete_log),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            log_actions.addWidget(button)
        log_layout.addLayout(log_actions)
        self.runtime_tabs.addTab(self.active_page, "活动服务")
        self.runtime_tabs.addTab(self.history_page, "启动历史")
        self.runtime_tabs.addTab(self.log_page, "运行日志")
        layout.addWidget(self.runtime_tabs, 1)
        self.active_table.selectionModel().selectionChanged.connect(self._runtime_selection_changed)
        self.history_table.selectionModel().selectionChanged.connect(self._runtime_selection_changed)
        return pane

    def _table_page(self, model, title: str, detail: str):
        page = QWidget()
        stack = QStackedWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        empty = EmptyState(title, detail)
        table = QTableView()
        table.setModel(model)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(False)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(True)
        stack.addWidget(empty)
        stack.addWidget(table)
        layout.addWidget(stack)
        page.stack = stack  # type: ignore[attr-defined]
        return page, table, empty

    # ---- Library and details -----------------------------------------

    def bind_records(self, records: list[ScriptRecord]) -> None:
        previous = (
            self.selected_record.fingerprint.canonical_path
            if self.selected_record else self.service.settings.last_selected_path
        )
        self.records = records
        self._rebuild_library(records, previous)
        self.library_count.setText(f"{len(records)} 个脚本")

    def _filter_library(self, query: str) -> None:
        records = self.service.search(query) if query.strip() else self.records
        selected = self.selected_record.fingerprint.canonical_path if self.selected_record else ""
        self._rebuild_library(records, selected)

    def _rebuild_library(self, records: list[ScriptRecord], preferred: str = "") -> None:
        self.library_model.clear()
        active_paths = {item.script_path for item in self.service.active_launches().values()}
        groups: dict[str, QStandardItem] = {}
        for label in ("★ 收藏", "最近使用", "正在运行", "llama.cpp", "Ollama", "通用/部分解析",
                      "按文件夹", "按标签", "按游戏/用途"):
            item = QStandardItem(label)
            item.setEditable(False)
            item.setSelectable(False)
            self.library_model.appendRow(item)
            groups[label] = item
        first_index = QModelIndex()
        preferred_index = QModelIndex()

        def add(parent: QStandardItem, record: ScriptRecord) -> QModelIndex:
            nonlocal first_index, preferred_index
            item = QStandardItem(self.service.display_name(record))
            item.setData(record.fingerprint.canonical_path, SCRIPT_ROLE)
            item.setToolTip(str(record.parsed.path))
            parent.appendRow(item)
            index = item.index()
            if not first_index.isValid():
                first_index = index
            if record.fingerprint.canonical_path == preferred and not preferred_index.isValid():
                preferred_index = index
            return index

        folder_nodes: dict[str, QStandardItem] = {}
        tag_nodes: dict[str, QStandardItem] = {}
        game_nodes: dict[str, QStandardItem] = {}
        backend_group = {
            Backend.LLAMA_CPP: groups["llama.cpp"],
            Backend.OLLAMA: groups["Ollama"],
            Backend.GENERIC: groups["通用/部分解析"],
        }
        for record in records:
            metadata = self.service.metadata(record)
            add(groups["★ 收藏"] if metadata.favorite else backend_group[record.parsed.backend], record)
            if metadata.last_run_at:
                add(groups["最近使用"], record)
            if str(record.parsed.path) in active_paths:
                add(groups["正在运行"], record)
            folder = str(record.parsed.path.parent)
            if folder not in folder_nodes:
                folder_nodes[folder] = QStandardItem(folder)
                folder_nodes[folder].setSelectable(False)
                groups["按文件夹"].appendRow(folder_nodes[folder])
            add(folder_nodes[folder], record)
            for tag in metadata.tags:
                if tag not in tag_nodes:
                    tag_nodes[tag] = QStandardItem(tag)
                    tag_nodes[tag].setSelectable(False)
                    groups["按标签"].appendRow(tag_nodes[tag])
                add(tag_nodes[tag], record)
            for game in [*metadata.games, *([metadata.category] if metadata.category else [])]:
                if game not in game_nodes:
                    game_nodes[game] = QStandardItem(game)
                    game_nodes[game].setSelectable(False)
                    groups["按游戏/用途"].appendRow(game_nodes[game])
                add(game_nodes[game], record)
        self.library_tree.expandToDepth(1)
        target = preferred_index if preferred_index.isValid() else first_index
        if target.isValid():
            self.library_tree.setCurrentIndex(target)
            self.library_tree.scrollTo(target)
            self._select_index(target)
        else:
            self.clear_selection()

    def _library_selection_changed(self, selected: QItemSelection, _deselected: QItemSelection) -> None:
        indexes = selected.indexes()
        if indexes:
            self._select_index(indexes[0])

    def _library_activated(self, index: QModelIndex) -> None:
        self._select_index(index)
        self.detail_tabs.setCurrentIndex(0)

    def _select_index(self, index: QModelIndex) -> None:
        key = index.data(SCRIPT_ROLE)
        if not key:
            return
        record = next(
            (item for item in self.records if item.fingerprint.canonical_path == key),
            None,
        )
        if record:
            self.select_record(record)

    def select_record(self, record: ScriptRecord) -> None:
        self.selected_record = record
        self.service.settings.last_selected_path = record.fingerprint.canonical_path
        self.detail_stack.setCurrentIndex(1)
        self._render_details(record)
        self._update_actions()

    def clear_selection(self) -> None:
        self.selected_record = None
        self.service.settings.last_selected_path = ""
        self.library_tree.clearSelection()
        self._update_empty_details()
        self._update_actions()

    def _update_empty_details(self) -> None:
        self.detail_stack.setCurrentIndex(0)

    def _render_details(self, record: ScriptRecord) -> None:
        parsed = record.parsed
        metadata = self.service.metadata(record)
        active = next(
            (item for item in self.service.active_launches().values()
             if item.script_path == str(parsed.path)),
            None,
        )
        self.display_name_label.setText(metadata.display_name or parsed.path.stem)
        self.filename_label.setText(f"{parsed.path.name}  •  {parsed.backend.value}")
        self._set_badge(self.parse_badge, f"解析：{parsed.confidence.value}", "Badge")
        run_style = "GoodBadge" if record.runnability.launch_allowed else "ErrorBadge"
        self._set_badge(
            self.runnable_badge, f"可运行性：{record.runnability.status.value}", run_style
        )
        runtime = (
            runtime_display_state(active.state)
            if active else RuntimeState.NOT_RUNNING.value
        )
        self._set_badge(
            self.runtime_badge, f"运行：{runtime}", "GoodBadge" if active else "Badge"
        )
        actual_port = active.actual_port if active else None
        api = runtime_api_address(parsed, actual_port) if actual_port else parsed.api_address
        api_diagnostics = (
            f"  API 检查状态：{active.api_status.value}\n"
            f"  检查 URL：{active.api_checked_url or '尚未检查'}\n"
            f"  最近 HTTP 状态：{active.api_last_http_status if active.api_last_http_status is not None else '无'}\n"
            f"  最近错误：{active.api_last_error or '无'}\n"
            f"  最近检查时间：{active.api_last_check_at or '无'}\n"
            f"  重试次数：{active.api_retry_count}\n"
            if active else
            "  API 检查状态：没有已验证的活动服务\n"
        )
        values = {
            "model": (parsed.model_name or "", ""),
            "model_path": (parsed.model_path or "", "file"),
            "executable": (parsed.executable or "", "file"),
            "configured_port": (parsed.configured_port, ""),
            "actual_port": (actual_port, ""),
            "api": (api or "", ""),
            "script": (str(parsed.path), "file"),
            "context": (parsed.context_size, ""),
        }
        for key, (value, kind) in values.items():
            self.copy_fields[key].set_value(value, kind)
        self.technical_text.setPlainText(
            "脚本\n"
            f"  完整路径：{parsed.path}\n"
            f"  修改时间：{parsed.modified_at or '未知'}\n"
            f"  SHA-256：{parsed.content_hash}\n\n"
            "服务器\n"
            f"  绑定主机：{parsed.bind_host or '未确定'}\n"
            f"  配置端口：{parsed.configured_port or '未确定'}\n"
            f"  实际端口：{actual_port or '未运行'}\n"
            f"  API：{api or '未确定'}\n\n"
            f"{api_diagnostics}\n"
            "上下文与性能\n"
            f"  上下文：{parsed.context_size or '未确定'}\n"
            f"  GPU 层：{self._localized_gpu_layers(parsed.gpu_layers)}\n"
            f"  参数：{parsed.parameters or '无'}\n\n"
            f"环境变量\n  {self._redacted_environment(parsed.environment)}\n\n"
            f"其他参数\n  {' '.join(parsed.other_arguments) or '无'}\n\n"
            f"启动方式\n  {self.service.effective_launch_mode(record).value}\n"
            f"  交互命令：{'、'.join(parsed.interactive_commands) or '未检测到'}"
        )
        warnings = [*record.runnability.warnings, *parsed.warnings, *parsed.dynamic_reasons]
        self.warnings_text.setPlainText(
            "\n\n".join(f"• {item}" for item in warnings) if warnings else "未发现静态警告。"
        )
        self.metadata_text.setPlainText(
            f"显示名：{metadata.display_name or '未设置'}\n"
            f"收藏：{'是' if metadata.favorite else '否'}\n"
            f"标签：{', '.join(metadata.tags) or '无'}\n"
            f"游戏：{', '.join(metadata.games) or '无'}\n"
            f"分类：{metadata.category or '无'}\n\n"
            f"目的\n{metadata.purpose or '无'}\n\n优势\n{metadata.strengths or '无'}\n\n"
            f"弱点\n{metadata.weaknesses or '无'}\n\n备注\n{metadata.notes or '无'}"
        )
        self.raw_text.setPlainText(redact(parsed.raw_text))
        self.evidence_text.setPlainText(
            "\n\n".join(
                f"第 {item.line_number} 行\n源行：{item.source_line}\n"
                f"规范化：{item.normalized_token}\n字段：{item.field}\n理由：{item.reason}"
                for item in parsed.evidence
            ) or "没有可显示的解析证据。"
        )

    @staticmethod
    def _set_badge(label: QLabel, text: str, object_name: str) -> None:
        label.setText(text)
        label.setObjectName(object_name)
        label.style().unpolish(label)
        label.style().polish(label)

    @staticmethod
    def _redacted_environment(environment: dict[str, str]) -> str:
        return "; ".join(
            f"{key}={'<已隐藏>' if 'KEY' in key.upper() else value}"
            for key, value in environment.items()
        ) or "无"

    # ---- Actions and async adapters ----------------------------------

    def _update_actions(self) -> None:
        record = self.selected_record
        active = self._active_for_selected()
        candidate = self._runtime_candidate_for_selected()
        has_record = record is not None
        state = candidate.state if candidate else RuntimeState.NOT_RUNNING
        starting = self._start_in_progress or state == RuntimeState.STARTING
        stopping = state == RuntimeState.STOPPING
        running = bool(active and not stopping)
        runnable = bool(
            record and record.runnability.launch_allowed
            and not starting and not stopping and not running
        )
        reasons = {
            "start": (
                "选择脚本后可启动" if not record else
                ("\n".join(record.runnability.warnings) or "当前配置不可运行")
                if not record.runnability.launch_allowed else
                "已有启动操作正在进行" if starting else
                "已有服务正在运行" if running else "启动原始脚本"
            ),
            "stop": "当前没有已验证的活动服务" if not active else "停止已验证的托管进程树",
            "restart": "当前没有已验证的活动服务" if not active else "安全停止后重新启动",
        }
        self.actions["start"].setEnabled(runnable)
        self.actions["start"].setText(
            "启动中……" if starting else
            "重试" if state in {RuntimeState.FAILED, RuntimeState.STOPPED} else "启动"
        )
        self.actions["start"].setToolTip(reasons["start"])
        stop_target = active or (
            candidate
            if candidate and candidate.verified and state == RuntimeState.STARTING
            else None
        )
        can_stop = bool(stop_target and stop_target.verified and not stopping)
        self.actions["stop"].setEnabled(can_stop)
        self.actions["stop"].setText("停止中……" if stopping else "停止")
        self.actions["stop"].setToolTip(reasons["stop"])
        self.actions["restart"].setEnabled(bool(running))
        self.actions["restart"].setText("重启")
        self.actions["restart"].setToolTip(reasons["restart"])
        self.actions["api"].setEnabled(bool(running))
        self.actions["api"].setToolTip(
            "使用实际运行地址重新检查 API" if running else "当前没有可检查的活动服务"
        )
        self.actions["copy_api"].setEnabled(bool(record and self.copy_fields["api"].value))
        self.actions["copy_api"].setToolTip(
            "复制当前完整 API 地址" if self.actions["copy_api"].isEnabled()
            else "当前没有可复制的 API 地址"
        )
        self.actions["open_api"].setEnabled(bool(record and self.copy_fields["api"].value))
        self.actions["open_api"].setToolTip(
            "使用系统默认程序打开当前 API 地址"
            if self.actions["open_api"].isEnabled() else "当前没有可打开的 API 地址"
        )
        self.actions["open_script"].setEnabled(has_record)
        self.actions["open_folder"].setEnabled(has_record)
        self.actions["open_log"].setEnabled(bool(self._selected_launch()))
        self.actions["rescan"].setEnabled(True)
        self._set_action_role("start", "start" if runnable else "neutral")
        self._set_action_role("stop", "danger" if can_stop else "neutral")
        self._set_action_role("restart", "warning" if running else "neutral")
        self._set_action_role("api", "info" if running else "neutral")
        self._set_action_role(
            "open_log",
            "error_hint" if state == RuntimeState.FAILED else "neutral",
        )

    def _set_action_role(self, key: str, role: str) -> None:
        button = self.actions[key]
        if button.property("semanticRole") == role:
            return
        button.setProperty("semanticRole", role)
        button.style().unpolish(button)
        button.style().polish(button)

    def _runtime_candidate_for_selected(self) -> ManagedLaunch | None:
        if not self.selected_record:
            return None
        matches = [
            item for item in self.service.launches.values()
            if item.script_path == str(self.selected_record.parsed.path)
        ]
        return max(matches, key=lambda item: item.started_at) if matches else None

    def _active_for_selected(self) -> ManagedLaunch | None:
        if not self.selected_record:
            return None
        return next(
            (item for item in self.service.active_launches().values()
             if item.script_path == str(self.selected_record.parsed.path)),
            None,
        )

    def _run_async(self, function, on_success, on_error=None, on_finished=None) -> Worker:
        worker = Worker(function)
        self._workers.add(worker)
        worker.signals.succeeded.connect(on_success)
        worker.signals.failed.connect(on_error or self._show_error)
        def finished() -> None:
            self._workers.discard(worker)
            if on_finished:
                on_finished()
        worker.signals.finished.connect(finished)
        QThreadPool.globalInstance().start(worker)
        return worker

    def rescan_async(self) -> None:
        self.actions["rescan"].setEnabled(False)
        self.statusBar().showMessage("正在扫描和解析脚本…")
        preferred = self.selected_record.fingerprint.canonical_path if self.selected_record else ""
        def success(records) -> None:
            self.bind_records(records)
            if preferred and not any(
                item.fingerprint.canonical_path == preferred for item in records
            ):
                self.clear_selection()
            self.statusBar().showMessage(f"扫描完成：{len(records)} 个脚本", 2500)
        self._run_async(
            self.service.rescan, success,
            on_finished=lambda: self.actions["rescan"].setEnabled(True),
        )

    def start_requested(self) -> None:
        record = self.selected_record
        if not record or self._start_in_progress or not record.runnability.launch_allowed:
            return
        self._start_in_progress = True
        self._update_actions()
        self.statusBar().showMessage("正在执行启动前端口检查…")
        self._run_async(
            lambda: self.service.inspect_port(record.parsed),
            lambda occupant: self._preflight_complete(record, occupant),
            on_finished=lambda: None,
        )

    def _preflight_complete(self, record: ScriptRecord, occupant) -> None:
        if occupant:
            self._handle_conflict(record, occupant)
        else:
            self._confirm_and_launch(record, None)

    def _handle_conflict(self, record: ScriptRecord, occupant) -> None:
        managed = (
            self.service.active_launches().get(occupant.managed_launch_id)
            if occupant.managed_launch_id else None
        )
        managed_record = next(
            (item for item in self.records if managed and str(item.parsed.path) == managed.script_path),
            None,
        )
        dialog = ConflictDialog(
            record, occupant, managed, managed_record, self.service.display_name(record),
            self.service.display_name(managed_record) if managed_record else "", self,
        )
        dialog.exec()
        choice = dialog.choice
        if choice == ConflictDecision.CANCEL:
            self._finish_start()
        elif choice == "process_info":
            TextDialog(
                "端口占用进程",
                f"PID：{occupant.pid or '未知'}\n进程：{occupant.process_name or '未知'}\n"
                f"路径：{occupant.executable or '不可访问'}\n\n"
                "该进程不由 LLMBatDesk 管理，不会自动终止。",
                self, wrap=True,
            ).exec()
            self._finish_start()
        elif choice == ConflictDecision.ALTERNATE_PORT:
            self._alternate_port(record)
        elif choice in {ConflictDecision.STOP_ONLY, ConflictDecision.STOP_AND_START}:
            self._run_async(
                lambda: self.service.handle_conflict_decision(choice, occupant),
                lambda result: (
                    self._confirm_and_launch(record, None)
                    if result == "start" else self._finish_start()
                ),
                on_finished=self.refresh_runtime_async,
            )

    def _alternate_port(self, record: ScriptRecord) -> None:
        if not record.parsed.port_source:
            self._show_error(ValueError(
                "无法安全覆盖端口："
                + (record.parsed.dynamic_reasons[0] if record.parsed.dynamic_reasons
                   else "没有唯一且静态的端口来源")
            ))
            self._finish_start()
            return
        suggested = min(65535, (record.parsed.configured_port or 8000) + 1)
        dialog = AlternatePortDialog(
            record, self.service.display_name(record), suggested,
            self.service.port_inspector.inspect, self.service.port_inspector.find_free,
            self.service.prepare_override, self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.override:
            self._confirm_and_launch(record, dialog.override)
        else:
            self._finish_start()

    def _confirm_and_launch(self, record: ScriptRecord, override) -> None:
        actual = override.new_port if override else record.parsed.configured_port
        details = (
            f"工作目录：{record.parsed.path.parent}\n"
            f"后端：{record.parsed.backend.value}\n模型：{record.parsed.model_name or '未确定'}\n"
            f"配置端口：{record.parsed.configured_port or '未确定'}\n"
            f"本次端口：{actual or '未确定'}\n\n"
            f"启动方式：{self.service.effective_launch_mode(record).value}\n"
            '固定机制：cmd.exe /d /s /c call "<script>"（shell=False）'
        )
        confirmed = False
        if self.service.should_confirm_launch(record, override=override):
            warnings = [
                *self.service.safety_confirmation_reasons(record, override=override),
                *record.parsed.warnings,
                *record.runnability.warnings,
            ]
            persistent = [
                reason for reason in self.service.safety_confirmation_reasons(
                    record, override=override
                )
                if reason not in {
                    "新脚本，尚未信任", "脚本内容已变化", "工作目录已变化"
                }
            ]
            dialog = TrustDialog(
                record, list(dict.fromkeys(warnings)), self, launch_details=details,
                future_confirmation_reasons=persistent,
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                if override:
                    override.path.unlink(missing_ok=True)
                self._finish_start()
                return
            confirmed = True
            if dialog.remember.isChecked():
                self.service.trust(record)
        self.statusBar().showMessage("正在创建启动进程…")
        self._run_async(
            lambda: self.service.launch(record, override, confirmed=confirmed),
            self._launch_complete,
            on_error=self._launch_error,
            on_finished=self._finish_start,
        )

    def _launch_complete(self, launch: ManagedLaunch) -> None:
        self.statusBar().showMessage(
            f"启动请求 {launch.launch_id[:8]} 已创建，等待服务器身份验证", 3500
        )
        self.refresh_runtime_async()
        record = next(
            (item for item in self.records if str(item.parsed.path) == launch.script_path),
            None,
        )
        if record:
            self._start_api_poll(launch, record)

    def _launch_error(self, error: Exception) -> None:
        if isinstance(error, PortConflictError) and self.selected_record:
            self._handle_conflict(self.selected_record, error.occupant)
            return
        self._show_error(error)

    def _finish_start(self) -> None:
        self._start_in_progress = False
        self._update_actions()

    def stop_requested(self) -> None:
        launch = self._active_for_selected()
        if launch is None:
            candidate = self._runtime_candidate_for_selected()
            launch = (
                candidate if candidate and candidate.verified
                and candidate.state == RuntimeState.STARTING else None
            )
        if not launch:
            return
        if QMessageBox.question(
            self, "停止已验证服务", "只会停止当前已验证属于该启动的进程树。继续？"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._run_async(
            lambda: self.service.stop(launch.launch_id),
            lambda _result: self.refresh_runtime_async(),
        )

    def restart_requested(self) -> None:
        launch, record = self._active_for_selected(), self.selected_record
        if not launch or not record:
            return
        confirmed = False
        if self.service.should_confirm_launch(record):
            warnings = self.service.safety_confirmation_reasons(record)
            dialog = TrustDialog(
                record, warnings, self,
                launch_details="将先安全停止当前已验证服务，再按当前设置重新启动。",
                future_confirmation_reasons=[
                    reason for reason in warnings
                    if reason not in {
                        "新脚本，尚未信任", "脚本内容已变化", "工作目录已变化"
                    }
                ],
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            confirmed = True
            if dialog.remember.isChecked():
                self.service.trust(record)

        def restart() -> ManagedLaunch:
            if not self.service.stop(launch.launch_id):
                raise RuntimeError("无法验证或停止原进程，已取消重启")
            return self.service.launch(record, confirmed=confirmed)

        self._run_async(
            restart,
            self._launch_complete,
        )

    def test_api_requested(self) -> None:
        launch, record = self._active_for_selected(), self.selected_record
        if not launch or not record:
            return
        self.statusBar().showMessage("正在执行轻量 API 检查…")
        self._run_async(
            lambda: self.service.check_api(launch, record.parsed),
            self._api_check_complete,
        )

    def _start_api_poll(self, launch: ManagedLaunch, record: ScriptRecord) -> None:
        if launch.launch_id in self._api_polling or launch.api_ready:
            return
        self._api_polling.add(launch.launch_id)
        self._run_async(
            lambda: self.service.poll_api_readiness(launch, record.parsed),
            self._api_check_complete,
            on_finished=lambda: self._api_poll_finished(launch.launch_id),
        )

    def _api_poll_finished(self, launch_id: str) -> None:
        self._api_polling.discard(launch_id)
        self.refresh_runtime_async()

    def _api_check_complete(self, result) -> None:
        self.statusBar().showMessage(result.detail, 3500)
        self._render_runtime_models()

    def refresh_runtime_async(self) -> None:
        if self._runtime_refresh_in_progress:
            return
        self._runtime_refresh_in_progress = True
        self._run_async(
            self.service.reconcile_runtimes,
            lambda _active: self._render_runtime_models(),
            on_finished=lambda: setattr(self, "_runtime_refresh_in_progress", False),
        )

    def _render_runtime_models(self) -> None:
        active = self.service.active_launches()
        history = self.service.history_entries()
        self.active_model.set_launches(active, self.service.records, self.service.display_name)
        self.history_model.set_history(history, self.service.records, self.service.display_name)
        self.active_page.stack.setCurrentIndex(1 if active else 0)  # type: ignore[attr-defined]
        self.history_page.stack.setCurrentIndex(1 if history else 0)  # type: ignore[attr-defined]
        self._update_actions()
        if self.selected_record:
            self._render_details(self.selected_record)
        for launch in active.values():
            if not launch.api_ready and launch.launch_id not in self._api_polling:
                record = next(
                    (
                        item for item in self.records
                        if str(item.parsed.path) == launch.script_path
                    ),
                    None,
                )
                if record and launch.api_status.value == "尚未检查":
                    self._start_api_poll(launch, record)

    def refresh_log_async(self) -> None:
        if self._log_refresh_in_progress:
            return
        launch = self._selected_launch()
        if not launch or not Path(launch.log_path).exists():
            return
        self._log_refresh_in_progress = True
        self._run_async(
            lambda: Path(launch.log_path).read_text(encoding="utf-8", errors="replace")[-200_000:],
            self._set_log_content,
            on_finished=lambda: setattr(self, "_log_refresh_in_progress", False),
        )

    def _set_log_content(self, content: str) -> None:
        self.log_text.set_log_text(content)
        error = next(
            (
                line for line in reversed(content.splitlines())
                if any(token in line.casefold() for token in (
                    "error", "failed", "cuda", "not found", "out of memory", "错误", "失败",
                ))
            ),
            "",
        )
        self.latest_error_field.set_value(error)

    # ---- Simple actions ----------------------------------------------

    def copy_api(self) -> None:
        self.copy_fields["api"].copy()

    def open_api_address(self) -> None:
        value = self.copy_fields["api"].value
        if value:
            QDesktopServices.openUrl(QUrl(value))

    def open_script(self) -> None:
        if self.selected_record:
            try:
                self.service.open_script(self.selected_record.parsed.path)
            except Exception as error:
                self._show_error(error)

    def open_folder(self) -> None:
        if self.selected_record:
            self._open_path(self.selected_record.parsed.path.parent)

    def open_latest_log(self) -> None:
        launch = self._selected_launch()
        if launch:
            self._open_path(Path(launch.log_path))

    def open_log_directory(self) -> None:
        path = self.service.data_dir / "logs"
        path.mkdir(parents=True, exist_ok=True)
        self._open_path(path)

    def _log_severity_changed(self) -> None:
        selected = self.log_severity.currentData()
        self.log_text.enabled_severities = (
            set(LogSeverity) if selected == "all" else {selected}
        )
        self.log_text.render_log()

    def _log_auto_scroll_changed(self, enabled: bool) -> None:
        self.log_text.set_auto_scroll(enabled)
        self.service.settings.log_auto_scroll = enabled

    def jump_to_latest_error(self) -> None:
        if not self.log_text.jump_to_latest_error():
            self.statusBar().showMessage("当前日志中没有检测到错误", 1800)

    def clear_log_view(self) -> None:
        self.log_text.clear_view()
        self.statusBar().showMessage("已清空显示；磁盘日志未删除", 2200)

    def cleanup_storage_async(self) -> None:
        self._run_async(
            self.service.cleanup_expired_logs,
            lambda result: self.statusBar().showMessage(
                f"日志清理完成：删除 {result.deleted_count} 个文件", 2200
            ) if result.deleted_count else None,
        )

    def show_settings(self) -> None:
        original_terminal = (
            self.service.settings.terminal_font_family,
            self.service.settings.terminal_font_size,
            self.service.settings.terminal_line_spacing_percent,
        )
        dialog = SettingsDialog(self.service.settings, self.service.data_dir, self)
        self.settings_dialog = dialog
        dialog.terminalAppearanceChanged.connect(self._apply_terminal_font)
        dialog.actionRequested.connect(
            lambda action: self._settings_action(dialog, action)
        )
        self._run_async(self.service.storage_stats, dialog.update_storage_stats)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.service.settings = dialog.settings
            self.service.settings_store.save(dialog.settings)
            apply_theme(QApplication.instance(), dialog.settings.theme)
            self.log_auto_scroll.setChecked(dialog.settings.log_auto_scroll)
            self.cleanup_storage_async()
        else:
            self._apply_terminal_font(*original_terminal)
        self.settings_dialog = None

    def _apply_terminal_font(
        self, family: str, point_size: int, line_spacing_percent: int
    ) -> None:
        if hasattr(self, "log_text"):
            self.log_text.configure_font(
                family, point_size, line_spacing_percent
            )

    def _settings_action(self, dialog: SettingsDialog, action: str) -> None:
        if action == "open_data":
            self.service.data_dir.mkdir(parents=True, exist_ok=True)
            self._open_path(self.service.data_dir)
            return
        if action == "delete_all_data":
            confirmation = DeleteUserDataDialog(self.service.data_dir, dialog)
            if confirmation.exec() == QDialog.DialogCode.Accepted:
                self._run_async(
                    lambda: self.service.delete_all_user_data(
                        confirmation.confirmation.text()
                    ),
                    lambda _result: (
                        self.statusBar().showMessage("全部用户数据已删除", 3500),
                        dialog.reject(),
                    ),
                )
            return
        functions = {
            "clear_all_logs": self.service.clear_all_logs,
            "cleanup_logs": self.service.cleanup_expired_logs,
            "clear_history": self.service.clear_launch_history,
            "clear_temporary": self.service.clear_temporary_files,
            "reset_ui": self.service.reset_ui_settings,
        }
        function = functions.get(action)
        if function is None:
            return
        self._run_async(
            function,
            lambda _result: self._settings_action_complete(dialog),
        )

    def _settings_action_complete(self, dialog: SettingsDialog) -> None:
        self.statusBar().showMessage("清理操作已完成", 2400)
        self._run_async(self.service.storage_stats, dialog.update_storage_stats)
        self._render_runtime_models()

    def _open_path(self, path: Path) -> None:
        try:
            self.service.open_path(path)
        except Exception as error:
            self._show_error(error)

    def _selected_launch(self) -> ManagedLaunch | None:
        for table, model in (
            (self.active_table, self.active_model), (self.history_table, self.history_model)
        ):
            indexes = table.selectionModel().selectedRows()
            if indexes:
                return model.launch_at(indexes[0].row())
        return self._active_for_selected()

    def _runtime_selection_changed(self, *_args) -> None:
        self.refresh_log_async()
        self._update_actions()

    def find_in_log(self, *, previous: bool = False) -> None:
        query = self.log_search.text()
        self.log_text.case_sensitive = self.log_case_sensitive.isChecked()
        if query and not self.log_text.find_text(query, previous=previous):
            self.statusBar().showMessage("日志中未找到该文本", 1800)

    def edit_metadata(self) -> None:
        if not self.selected_record:
            return
        record = self.selected_record
        dialog = MetadataDialog(self.service.metadata(record), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.service.save_metadata(dialog.metadata)
            self.bind_records(self.records)

    def add_root(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "选择扫描根目录")
        if value:
            self.service.add_root(Path(value))
            self.rescan_async()

    def add_script(self) -> None:
        value, _filter = QFileDialog.getOpenFileName(
            self, "添加 BAT/CMD", "", "批处理脚本 (*.bat *.cmd)"
        )
        if value:
            try:
                self.service.add_individual(Path(value))
            except Exception as error:
                self._show_error(error)
                return
            self.bind_records(list(self.service.records.values()))

    def toggle_theme(self) -> None:
        self.service.settings.theme = (
            "light" if self.service.settings.theme == "dark" else "dark"
        )
        apply_theme(QApplication.instance(), self.service.settings.theme)
        self.service.settings_store.save(self.service.settings)

    def show_dpi_diagnostics(self) -> None:
        TextDialog("DPI 与字体诊断", self.dpi_diagnostics, self, wrap=True).exec()

    def show_usage_guide(self) -> None:
        TextDialog("使用说明", USAGE_GUIDE, self, wrap=True).exec()

    def show_faq(self) -> None:
        TextDialog("常见问题", FAQ, self, wrap=True).exec()

    def show_security(self) -> None:
        TextDialog("安全机制与风险提示", SECURITY_GUIDE, self, wrap=True).exec()

    def show_about(self) -> None:
        TextDialog("关于 LLMBatDesk", about_text(), self, wrap=True).exec()

    @staticmethod
    def _localized_gpu_layers(value: int | str | None) -> str:
        if value == "all":
            return "全部"
        if value == "auto":
            return "自动"
        return str(value) if value is not None else "未确定"

    def _show_error(self, error: Exception) -> None:
        self.statusBar().showMessage(str(error), 4000)
        TextDialog("详细错误", str(error), self, wrap=True).exec()

    def _tab_changed(self, index: int) -> None:
        self.service.settings.qt_selected_tab = index

    def _runtime_tab_changed(self, index: int) -> None:
        self.service.settings.qt_runtime_tab = index

    # ---- Persistence and close ---------------------------------------

    def _restore_window_state(self) -> None:
        settings = self.service.settings
        try:
            if settings.qt_geometry:
                self.restoreGeometry(base64.b64decode(settings.qt_geometry))
            if settings.qt_window_state:
                self.restoreState(base64.b64decode(settings.qt_window_state))
            if settings.qt_splitter_state:
                self.main_splitter.restoreState(base64.b64decode(settings.qt_splitter_state))
        except (ValueError, TypeError):
            pass
        self.detail_tabs.setCurrentIndex(min(settings.qt_selected_tab, self.detail_tabs.count() - 1))
        self.runtime_tabs.setCurrentIndex(min(settings.qt_runtime_tab, self.runtime_tabs.count() - 1))

    def save_ui_state(self) -> None:
        settings = self.service.settings
        settings.qt_geometry = base64.b64encode(bytes(self.saveGeometry())).decode("ascii")
        settings.qt_window_state = base64.b64encode(bytes(self.saveState())).decode("ascii")
        settings.qt_splitter_state = base64.b64encode(
            bytes(self.main_splitter.saveState())
        ).decode("ascii")
        settings.window_maximized = self.isMaximized()
        settings.qt_selected_tab = self.detail_tabs.currentIndex()
        settings.qt_runtime_tab = self.runtime_tabs.currentIndex()
        settings.qt_column_widths = {
            "active": [self.active_table.columnWidth(i) for i in range(self.active_model.columnCount())],
            "history": [self.history_table.columnWidth(i) for i in range(self.history_model.columnCount())],
        }
        self.service.settings_store.save(settings)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.save_ui_state()
        active = list(self.service.active_launches().values())
        if not active:
            event.accept()
            return
        box = QMessageBox(self)
        box.setWindowTitle("仍有已验证服务运行")
        box.setText(f"当前有 {len(active)} 个已验证托管服务。")
        leave = box.addButton("保留模型服务器运行并退出", QMessageBox.ButtonRole.AcceptRole)
        stop = box.addButton("停止托管模型服务器并退出", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == cancel:
            event.ignore()
        elif box.clickedButton() == stop:
            for launch in active:
                self.service.stop(launch.launch_id)
            event.accept()
        else:
            self.service.leave_running()
            event.accept()
