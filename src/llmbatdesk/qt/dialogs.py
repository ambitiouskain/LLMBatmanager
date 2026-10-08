from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QFileDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from ..domain.models import (
    EditorMode, LaunchConfirmationMode, LaunchMode, LaunchModeOverride, ManagedLaunch, Metadata,
    PortOccupant, ScriptDiscoveryInfo, ScriptRecord, ScriptRemovalMode,
)
from ..runtime.cleanup import StorageStats
from ..runtime.override import OverrideResult
from ..runtime.ports import ConflictDecision
from ..runtime.logging import redact
from .tasks import Worker
from ..settings import AppSettings, resolve_editor_executable
from .terminal_log import default_terminal_font_family


class TextDialog(QDialog):
    def __init__(self, title: str, content: str, parent=None, *, wrap: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.resize(900, 650)
        self.setMinimumSize(560, 360)
        layout = QVBoxLayout(self)
        self.editor = QPlainTextEdit(content)
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if wrap
            else QPlainTextEdit.LineWrapMode.NoWrap
        )
        self.editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.editor, 1)
        buttons = QDialogButtonBox()
        copy_button = buttons.addButton("复制全部", QDialogButtonBox.ButtonRole.ActionRole)
        copy_button.clicked.connect(lambda: self.editor.selectAll())
        copy_button.clicked.connect(self.editor.copy)
        close_button = buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        close_button.clicked.connect(self.reject)
        layout.addWidget(buttons)


class ScriptRemovalDialog(QDialog):
    def __init__(
        self, record: ScriptRecord, display_name: str,
        source: ScriptDiscoveryInfo, parent=None,
    ) -> None:
        super().__init__(parent)
        self.choice: ScriptRemovalMode | None = None
        self.setWindowTitle("移除脚本")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        heading = QLabel(f"移除脚本：{display_name}")
        heading.setObjectName("ErrorBadge")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        path = QLabel(str(record.parsed.path))
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path.setWordWrap(True)
        layout.addWidget(path)
        details = QLabel(
            f"发现来源：{source.source_label}\n"
            f"所属扫描目录：{source.owning_root or '否'}\n"
            f"单独添加：{'是' if source.individually_added else '否'}"
        )
        details.setWordWrap(True)
        layout.addWidget(details)
        notice = QLabel(
            "默认操作不会删除脚本文件。启动历史和历史日志会保留，"
            "脚本专属元数据与信任会被清除。"
        )
        notice.setObjectName("WarnBadge")
        notice.setWordWrap(True)
        layout.addWidget(notice)
        buttons = QDialogButtonBox()
        first_label = (
            "忽略此脚本，不再通过目录扫描显示"
            if source.discovered_from_root
            else "仅从脚本库移除"
        )
        self.library_only = buttons.addButton(
            first_label, QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.recycle = buttons.addButton(
            "将脚本文件移至 Windows 回收站",
            QDialogButtonBox.ButtonRole.DestructiveRole,
        )
        cancel = buttons.addButton(
            "取消", QDialogButtonBox.ButtonRole.RejectRole
        )
        self.library_only.setDefault(True)
        self.library_only.clicked.connect(
            lambda: self._choose(ScriptRemovalMode.LIBRARY_ONLY)
        )
        self.recycle.clicked.connect(
            lambda: self._choose(ScriptRemovalMode.RECYCLE_BIN)
        )
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _choose(self, choice: ScriptRemovalMode) -> None:
        self.choice = choice
        self.accept()


class IgnoredScriptsDialog(QDialog):
    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.changed = False
        self.setWindowTitle("管理已忽略脚本")
        self.resize(820, 430)
        self.setMinimumSize(620, 320)
        layout = QVBoxLayout(self)
        notice = QLabel(
            "忽略记录只匹配所属扫描目录内的一个规范脚本路径。取消忽略不会执行脚本。"
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ("脚本路径", "扫描根目录", "当前状态", "忽略日期")
        )
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 310)
        self.table.setColumnWidth(1, 230)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 150)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        unignore = QPushButton("取消忽略")
        unignore.clicked.connect(self._unignore)
        cleanup = QPushButton("清理不存在的记录")
        cleanup.clicked.connect(self._cleanup)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        actions.addWidget(unignore)
        actions.addWidget(cleanup)
        actions.addStretch()
        actions.addWidget(close)
        layout.addLayout(actions)
        self.status = QLabel()
        self.status.setObjectName("Muted")
        layout.addWidget(self.status)
        self._refresh()

    def _refresh(self) -> None:
        records = self.service.ignored_scripts()
        self.table.setRowCount(len(records))
        for row, item in enumerate(records):
            values = (
                item.canonical_path,
                item.root_path,
                "存在" if Path(item.canonical_path).is_file() else "不存在",
                item.ignored_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, item.canonical_path)
                    cell.setToolTip(item.canonical_path)
                self.table.setItem(row, column, cell)

    def _selected_path(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    def _unignore(self) -> None:
        path = self._selected_path()
        if not path:
            self.status.setText("请先选择一条忽略记录")
            return
        if self.service.cancel_ignore(path):
            self.changed = True
            self.status.setText("已取消忽略；不会自动执行脚本")
            self._refresh()

    def _cleanup(self) -> None:
        count = self.service.cleanup_missing_ignores()
        self.changed = self.changed or count > 0
        self.status.setText(f"已清理 {count} 条不存在的记录")
        self._refresh()


class TrustDialog(QDialog):
    def __init__(
        self, record: ScriptRecord, warnings: list[str], parent=None,
        *, launch_details: str = "", future_confirmation_reasons: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("信任当前脚本内容")
        self.resize(820, 650)
        self.setMinimumSize(560, 400)
        layout = QVBoxLayout(self)
        title = QLabel("BAT/CMD 可以执行任意命令。解析成功不代表脚本安全。")
        title.setObjectName("WarnBadge")
        title.setWordWrap(True)
        layout.addWidget(title)
        path = QLabel(str(record.parsed.path))
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path.setWordWrap(True)
        layout.addWidget(path)
        if launch_details:
            details = QLabel(launch_details)
            details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            details.setWordWrap(True)
            layout.addWidget(details)
        if warnings:
            warning = QLabel("\n".join(f"• {item}" for item in warnings))
            warning.setWordWrap(True)
            layout.addWidget(warning)
        preview = QPlainTextEdit(redact(record.parsed.raw_text))
        preview.setReadOnly(True)
        preview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(preview, 1)
        self.remember = QCheckBox("在脚本内容不变时，不再询问")
        self.remember.setChecked(True)
        layout.addWidget(self.remember)
        if future_confirmation_reasons:
            notice = QLabel(
                "注意：即使记住本次信任，以下安全规则以后仍会要求确认：\n"
                + "\n".join(f"• {reason}" for reason in future_confirmation_reasons)
            )
            notice.setObjectName("WarnBadge")
            notice.setWordWrap(True)
            layout.addWidget(notice)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Yes).setText("信任此内容哈希并继续")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ConflictDialog(QDialog):
    def __init__(
        self,
        record: ScriptRecord,
        occupant: PortOccupant,
        managed_launch: ManagedLaunch | None,
        managed_record: ScriptRecord | None,
        display_name: str,
        managed_display_name: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.choice = ConflictDecision.CANCEL
        self.setWindowTitle(f"端口 {occupant.port} 冲突")
        self.resize(760, 570)
        self.setMinimumSize(580, 430)
        layout = QVBoxLayout(self)
        heading = QLabel(f"端口 {occupant.port} 当前已被占用")
        heading.setObjectName("Hero")
        layout.addWidget(heading)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(
            "当前占用\n"
            f"  管理状态：{'已验证的 LLMBatDesk 服务' if managed_launch else '外部或无法验证'}\n"
            f"  显示名：{managed_display_name or '未知'}\n"
            f"  模型：{managed_record.parsed.model_name if managed_record else '未知'}\n"
            f"  PID：{occupant.pid or '未知'}\n"
            f"  实际端口：{managed_launch.actual_port if managed_launch else occupant.port}\n"
            f"  进程：{occupant.process_name or '未知'}\n"
            f"  路径：{occupant.executable or '不可访问'}\n\n"
            "目标脚本\n"
            f"  显示名：{display_name}\n"
            f"  模型：{record.parsed.model_name or '未指定'}\n"
            f"  配置端口：{record.parsed.configured_port or '未知'}"
        )
        layout.addWidget(text, 1)
        choices = (
            (
                ("停止当前托管服务器并启动目标脚本", ConflictDecision.STOP_AND_START),
                ("仅停止当前托管服务器", ConflictDecision.STOP_ONLY),
                ("在其他端口启动目标脚本", ConflictDecision.ALTERNATE_PORT),
                ("取消", ConflictDecision.CANCEL),
            )
            if managed_launch else
            (
                ("在其他端口启动目标脚本", ConflictDecision.ALTERNATE_PORT),
                ("查看进程信息", "process_info"),
                ("取消", ConflictDecision.CANCEL),
            )
        )
        for label, value in choices:
            button = QPushButton(label)
            if value == ConflictDecision.ALTERNATE_PORT:
                button.setObjectName("PrimaryButton")
            button.clicked.connect(lambda _checked=False, selected=value: self._select(selected))
            layout.addWidget(button)

    def _select(self, choice: str) -> None:
        self.choice = choice
        self.accept()


class AlternatePortDialog(QDialog):
    def __init__(
        self,
        record: ScriptRecord,
        display_name: str,
        suggested_port: int,
        inspect_port,
        find_free,
        prepare_override,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.record = record
        self.inspect_port = inspect_port
        self.find_free = find_free
        self.prepare_override = prepare_override
        self.override: OverrideResult | None = None
        self._workers: set[Worker] = set()
        self._generation_serial = 0
        self._launch_when_ready = False
        self._generation_key: tuple[int, int] | None = None
        self.setWindowTitle("临时替代端口")
        self.resize(760, 620)
        self.setMinimumSize(580, 440)
        layout = QVBoxLayout(self)
        heading = QLabel("临时端口覆盖")
        heading.setObjectName("Hero")
        layout.addWidget(heading)
        explanation = QLabel(
            f"目标脚本：{display_name}\n原始配置端口：{record.parsed.configured_port}\n\n"
            "默认仅影响本次启动。原始 BAT/CMD 保持逐字节不变，"
            "临时副本只替换解析器验证过的端口字面量。"
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        row = QHBoxLayout()
        row.addWidget(QLabel("新端口"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(suggested_port)
        row.addWidget(self.port_spin)
        check_button = QPushButton("检查可用性")
        self.check_button = check_button
        check_button.clicked.connect(self.check_availability_async)
        row.addWidget(check_button)
        auto_button = QPushButton("自动选择")
        self.auto_button = auto_button
        auto_button.clicked.connect(self.auto_select_async)
        row.addWidget(auto_button)
        row.addStretch()
        layout.addLayout(row)
        self.availability = QLabel()
        layout.addWidget(self.availability)
        self.patch_summary = QLabel("正在检查端口并安全生成临时脚本…")
        self.patch_summary.setWordWrap(True)
        layout.addWidget(self.patch_summary)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.diff.setVisible(False)
        layout.addWidget(self.diff, 1)
        buttons = QDialogButtonBox()
        self.generate_button = buttons.addButton(
            "查看完整差异", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.generate_button.setEnabled(False)
        self.launch_button = buttons.addButton(
            "使用此临时端口", QDialogButtonBox.ButtonRole.AcceptRole
        )
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.launch_button.setObjectName("PrimaryButton")
        self.launch_button.setEnabled(False)
        self.generate_button.clicked.connect(
            lambda: self.diff.setVisible(not self.diff.isVisible())
        )
        self.launch_button.clicked.connect(self._use_selected_port)
        cancel_button.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.port_spin.valueChanged.connect(self._invalidate)
        self.check_availability_async()

    def _invalidate(self) -> None:
        self._generation_serial += 1
        self._generation_key = None
        if self.override:
            self.override.path.unlink(missing_ok=True)
        self.override = None
        self.launch_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.diff.clear()
        self.diff.setVisible(False)
        self.patch_summary.setText("端口已变化，正在重新验证并生成临时脚本…")
        QTimer.singleShot(120, self.check_availability_async)

    def check_availability(self) -> bool:
        """Synchronous adapter used by deterministic tests; UI uses the async slot."""
        occupant = self.inspect_port(self.port_spin.value())
        self._set_availability(occupant)
        return occupant is None

    def _set_availability(self, occupant) -> None:
        available = occupant is None
        self.availability.setText(
            "✓ 端口可用" if available else f"✕ 已被 PID {occupant.pid or '未知'} 占用"
        )
        self.availability.setObjectName("GoodBadge" if available else "ErrorBadge")
        self.availability.style().unpolish(self.availability)
        self.availability.style().polish(self.availability)

    def _run_async(self, function, success, finished=None, failure=None) -> None:
        worker = Worker(function)
        self._workers.add(worker)
        worker.signals.succeeded.connect(success)
        worker.signals.failed.connect(failure or self._generation_failed)
        def done() -> None:
            self._workers.discard(worker)
            if finished:
                finished()
        worker.signals.finished.connect(done)
        QThreadPool.globalInstance().start(worker)

    def check_availability_async(self) -> None:
        self.check_button.setEnabled(False)
        value = self.port_spin.value()
        serial = self._generation_serial
        self._run_async(
            lambda: self.inspect_port(value),
            lambda occupant: self._availability_ready(value, serial, occupant),
            lambda: self.check_button.setEnabled(True),
        )

    def _availability_ready(self, port: int, serial: int, occupant) -> None:
        if serial != self._generation_serial or port != self.port_spin.value():
            return
        self._set_availability(occupant)
        if occupant:
            self.patch_summary.setText("端口被占用，无法生成临时启动脚本。")
            self.launch_button.setEnabled(False)
            return
        self._begin_generation(port, serial)

    def auto_select_async(self) -> None:
        self.auto_button.setEnabled(False)
        self._run_async(
            lambda: self.find_free((self.record.parsed.configured_port or 8000) + 1),
            lambda port: (self.port_spin.setValue(port), self.check_availability_async()),
            lambda: self.auto_button.setEnabled(True),
        )

    def generate(self) -> None:
        """Synchronous adapter used by deterministic tests; UI uses the async slot."""
        if not self.check_availability():
            return
        try:
            self.override = self.prepare_override(self.record, self.port_spin.value())
        except Exception as error:
            self.diff.setPlainText(str(error))
            self.patch_summary.setText(f"临时脚本生成失败：{error}")
            self.launch_button.setEnabled(False)
            return
        self._show_override(self.override)

    def generate_async(self) -> None:
        self.check_availability_async()

    def _begin_generation(self, port: int, serial: int) -> None:
        key = (serial, port)
        if self._generation_key == key:
            return
        self._generation_key = key
        self.patch_summary.setText("端口可用，正在安全生成并验证临时脚本…")
        self._run_async(
            lambda: self.prepare_override(self.record, port),
            lambda override: self._override_ready(override, port, serial),
            failure=self._generation_failed,
        )

    def _override_ready(
        self, override: OverrideResult, port: int, serial: int
    ) -> None:
        if self._generation_key == (serial, port):
            self._generation_key = None
        if serial != self._generation_serial or port != self.port_spin.value():
            override.path.unlink(missing_ok=True)
            return
        if self.override:
            self.override.path.unlink(missing_ok=True)
        self.override = override
        self._show_override(override)
        if self._launch_when_ready:
            self._launch_when_ready = False
            self.accept()

    def _show_override(self, override: OverrideResult) -> None:
        original = self.record.parsed.configured_port
        self.patch_summary.setText(
            f"--port {original} → --port {override.new_port}\n"
            "✓ 临时脚本已安全生成并验证\n"
            "✓ 原始 BAT/CMD 保持逐字节不变"
        )
        self.diff.setPlainText(f"临时副本：{override.path}\n\n{override.diff}")
        self.generate_button.setEnabled(True)
        self.launch_button.setEnabled(True)

    def _generation_failed(self, error: Exception) -> None:
        self._generation_key = None
        self.override = None
        self._launch_when_ready = False
        self.launch_button.setEnabled(False)
        self.generate_button.setEnabled(False)
        self.patch_summary.setText(f"临时脚本生成失败：{error}")
        self.diff.setPlainText(str(error))

    def _use_selected_port(self) -> None:
        if self.override and self.override.new_port == self.port_spin.value():
            self.accept()
            return
        self._launch_when_ready = True
        self.patch_summary.setText("正在完成安全生成与验证，完成后将继续…")
        self.check_availability_async()

    def reject(self) -> None:
        if self.override:
            self.override.path.unlink(missing_ok=True)
            self.override = None
        super().reject()


class MetadataDialog(QDialog):
    def __init__(self, metadata: Metadata, parent=None) -> None:
        super().__init__(parent)
        self.metadata = metadata.model_copy(deep=True)
        self.setWindowTitle("元数据与笔记")
        self.resize(760, 650)
        self.setMinimumSize(560, 430)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        self.display_name = QLineEdit(metadata.display_name)
        self.tags = QLineEdit(", ".join(metadata.tags))
        self.games = QLineEdit(", ".join(metadata.games))
        self.category = QLineEdit(metadata.category)
        self.favorite = QCheckBox("收藏")
        self.favorite.setChecked(metadata.favorite)
        self.launch_mode = QComboBox()
        self.launch_mode.addItem("使用全局启动方式", LaunchModeOverride.GLOBAL)
        self.launch_mode.addItem("始终后台启动", LaunchModeOverride.BACKGROUND)
        self.launch_mode.addItem("始终显示控制台", LaunchModeOverride.VISIBLE)
        self.launch_mode.setCurrentIndex(
            max(0, self.launch_mode.findData(metadata.launch_mode_override))
        )
        self.text_fields: dict[str, QTextEdit] = {}
        form.addRow("显示名", self.display_name)
        form.addRow("标签（逗号分隔）", self.tags)
        form.addRow("游戏（逗号分隔）", self.games)
        form.addRow("用途分类", self.category)
        form.addRow("", self.favorite)
        form.addRow("脚本启动方式", self.launch_mode)
        for key, label in (
            ("purpose", "目的"), ("strengths", "优势"), ("weaknesses", "弱点"), ("notes", "备注")
        ):
            editor = QTextEdit(getattr(metadata, key))
            editor.setMinimumHeight(80)
            self.text_fields[key] = editor
            form.addRow(label, editor)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _save(self) -> None:
        self.metadata.display_name = self.display_name.text().strip()
        self.metadata.tags = [item.strip() for item in self.tags.text().split(",") if item.strip()]
        self.metadata.games = [item.strip() for item in self.games.text().split(",") if item.strip()]
        self.metadata.category = self.category.text().strip()
        self.metadata.favorite = self.favorite.isChecked()
        self.metadata.launch_mode_override = self.launch_mode.currentData()
        for key, editor in self.text_fields.items():
            setattr(self.metadata, key, editor.toPlainText().strip())
        self.accept()


def _format_size(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    if value < 1024 * 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} MB"
    return f"{value / 1024 / 1024 / 1024:.2f} GB"


class SettingsDialog(QDialog):
    actionRequested = Signal(str)
    terminalAppearanceChanged = Signal(str, int, int)

    def __init__(self, settings: AppSettings, data_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings.model_copy(deep=True)
        self.setWindowTitle("设置")
        self.resize(850, 650)
        self.setMinimumSize(650, 480)
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self.tabs.addTab(self._general_page(), "常规")
        self.tabs.addTab(self._launch_page(), "启动")
        self.tabs.addTab(self._log_page(), "日志")
        self.tabs.addTab(self._storage_page(data_dir), "存储与清理")
        self.tabs.addTab(self._appearance_page(), "外观")
        self.tabs.addTab(self._extensions_page(), "功能扩展")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _general_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.editor_mode = QComboBox()
        self.editor_mode.addItem("使用 Windows 默认关联程序", EditorMode.SYSTEM_DEFAULT)
        self.editor_mode.addItem("使用指定编辑器", EditorMode.CUSTOM)
        self.editor_mode.setCurrentIndex(
            max(0, self.editor_mode.findData(self.settings.editor_mode))
        )
        self.editor_mode.setToolTip(
            "决定“打开脚本”使用 Windows 文件关联，还是指定的文本编辑器；"
            "与 llama.cpp、Ollama 或模型路径无关。"
        )
        form.addRow("打开脚本方式", self.editor_mode)
        editor_row = QHBoxLayout()
        self.editor_path = QLineEdit(self.settings.editor_path)
        self.editor_path.setReadOnly(True)
        self.editor_path.setPlaceholderText("尚未选择编辑器可执行文件")
        self.editor_path.setToolTip("只显示编辑器 EXE 路径，不会执行任意命令参数")
        editor_row.addWidget(self.editor_path, 1)
        self.editor_browse = QPushButton("浏览……")
        self.editor_browse.clicked.connect(self._browse_editor)
        editor_row.addWidget(self.editor_browse)
        self.editor_reset = QPushButton("恢复默认")
        self.editor_reset.clicked.connect(self._reset_editor)
        editor_row.addWidget(self.editor_reset)
        form.addRow("指定编辑器", editor_row)
        self.editor_error = QLabel()
        self.editor_error.setObjectName("ErrorBadge")
        self.editor_error.setWordWrap(True)
        # Use a spanning row so hiding the banner collapses the complete row.
        # An empty label-role widget in QFormLayout can otherwise retain height.
        form.addRow(self.editor_error)
        self.editor_mode.currentIndexChanged.connect(self._editor_mode_changed)
        self.editor_path.textChanged.connect(self._editor_mode_changed)
        self._editor_mode_changed()
        return page

    def _launch_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.launch_mode = QComboBox()
        self.launch_mode.addItem("后台启动，不显示控制台", LaunchMode.BACKGROUND)
        self.launch_mode.addItem("显示原始控制台窗口", LaunchMode.VISIBLE)
        self.launch_mode.setCurrentIndex(
            max(0, self.launch_mode.findData(self.settings.launch_mode))
        )
        form.addRow("脚本启动方式", self.launch_mode)
        explanation = QLabel(
            "后台模式继续捕获 stdout/stderr 且不会显示 cmd.exe。显示控制台时，"
            "日志捕获可能受脚本和 Windows 控制台行为限制或重复。"
        )
        explanation.setWordWrap(True)
        form.addRow("", explanation)
        self.confirm_mode = QComboBox()
        self.confirm_mode.addItem("每次启动都确认", LaunchConfirmationMode.ALWAYS)
        self.confirm_mode.addItem(
            "首次启动或脚本变化后确认", LaunchConfirmationMode.FIRST_OR_CHANGED
        )
        self.confirm_mode.addItem(
            "对已信任且未变化的脚本不再确认", LaunchConfirmationMode.TRUSTED_SKIP
        )
        self.confirm_mode.setCurrentIndex(
            max(0, self.confirm_mode.findData(self.settings.launch_confirmation_mode))
        )
        form.addRow("启动确认", self.confirm_mode)
        self.api_timeout = QSpinBox()
        self.api_timeout.setRange(5, 3600)
        self.api_timeout.setValue(int(self.settings.api_startup_timeout_seconds))
        self.api_timeout.setSuffix(" 秒")
        form.addRow("API 启动检查超时", self.api_timeout)
        return page

    def _log_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.retention = QSpinBox()
        self.retention.setRange(1, 3650)
        self.retention.setValue(self.settings.log_retention_days or 30)
        self.retention_unlimited = QCheckBox("不限保留天数")
        self.retention_unlimited.setChecked(self.settings.log_retention_days is None)
        form.addRow("日志保留天数", self.retention)
        form.addRow("", self.retention_unlimited)
        self.max_mb = QSpinBox()
        self.max_mb.setRange(1, 102400)
        self.max_mb.setValue(self.settings.log_max_total_mb or 200)
        self.max_mb_unlimited = QCheckBox("不限日志总大小")
        self.max_mb_unlimited.setChecked(self.settings.log_max_total_mb is None)
        form.addRow("最大日志总大小", self.max_mb)
        form.addRow("", self.max_mb_unlimited)
        self.max_count = QSpinBox()
        self.max_count.setRange(1, 100000)
        self.max_count.setValue(self.settings.log_max_count or 100)
        self.max_count_unlimited = QCheckBox("不限日志文件数量")
        self.max_count_unlimited.setChecked(self.settings.log_max_count is None)
        form.addRow("最大日志数量", self.max_count)
        form.addRow("", self.max_count_unlimited)
        self.terminal_font_family = QComboBox()
        self.terminal_font_family.addItems(sorted(QFontDatabase.families()))
        selected_family = (
            self.settings.terminal_font_family or default_terminal_font_family()
        )
        self.terminal_font_family.setCurrentText(selected_family)
        self.terminal_font_size = QSpinBox()
        self.terminal_font_size.setRange(8, 24)
        self.terminal_font_size.setSuffix(" pt")
        self.terminal_font_size.setValue(self.settings.terminal_font_size)
        self.terminal_line_spacing = QSpinBox()
        self.terminal_line_spacing.setRange(80, 200)
        self.terminal_line_spacing.setSuffix(" %")
        self.terminal_line_spacing.setValue(
            self.settings.terminal_line_spacing_percent
        )
        terminal_reset = QPushButton("恢复日志字体默认值")
        terminal_reset.clicked.connect(self._reset_terminal_font)
        form.addRow("运行日志字体", self.terminal_font_family)
        form.addRow("运行日志字号", self.terminal_font_size)
        form.addRow("运行日志行距", self.terminal_line_spacing)
        form.addRow("", terminal_reset)
        for widget in (
            self.terminal_font_family, self.terminal_font_size,
            self.terminal_line_spacing,
        ):
            signal = (
                widget.currentTextChanged
                if isinstance(widget, QComboBox) else widget.valueChanged
            )
            signal.connect(self._emit_terminal_appearance)
        return page

    def _browse_editor(self) -> None:
        value, _selected = QFileDialog.getOpenFileName(
            self, "选择文本编辑器可执行文件", "", "Windows 程序 (*.exe)"
        )
        if value:
            self.editor_path.setText(value)
            self.editor_mode.setCurrentIndex(
                self.editor_mode.findData(EditorMode.CUSTOM)
            )
            self._editor_mode_changed()

    def _reset_editor(self) -> None:
        self.editor_path.clear()
        self.editor_mode.setCurrentIndex(
            self.editor_mode.findData(EditorMode.SYSTEM_DEFAULT)
        )
        self._editor_mode_changed()

    def _editor_mode_changed(self, *_args) -> None:
        custom = self.editor_mode.currentData() == EditorMode.CUSTOM
        self.editor_path.setEnabled(custom)
        self.editor_browse.setEnabled(custom)
        if not custom:
            self._set_editor_error("")
            return
        configured_path = self.editor_path.text().strip()
        if not configured_path:
            self._set_editor_error(
                "尚未选择指定编辑器。请选择有效的 .exe 可执行文件。"
            )
            return
        if resolve_editor_executable(configured_path) is None:
            self._set_editor_error(
                "指定编辑器不存在或不是有效的可执行文件。请重新选择 .exe 文件。"
            )
            return
        self._set_editor_error("")

    def _set_editor_error(self, message: str) -> None:
        """Show only actionable editor validation; an empty banner takes no space."""
        self.editor_error.setText(message)
        self.editor_error.setVisible(bool(message))

    def _reset_terminal_font(self) -> None:
        self.terminal_font_family.setCurrentText(default_terminal_font_family())
        self.terminal_font_size.setValue(11)
        self.terminal_line_spacing.setValue(100)
        self._emit_terminal_appearance()

    def _emit_terminal_appearance(self, *_args) -> None:
        self.terminalAppearanceChanged.emit(
            self.terminal_font_family.currentText(),
            self.terminal_font_size.value(),
            self.terminal_line_spacing.value(),
        )

    def _storage_page(self, data_dir: Path) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        path = QLabel(str(data_dir))
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path.setWordWrap(True)
        layout.addWidget(QLabel("完整用户数据目录"))
        layout.addWidget(path)
        self.storage_summary = QLabel("正在计算存储占用…")
        self.storage_summary.setWordWrap(True)
        layout.addWidget(self.storage_summary)
        actions = QGroupBox("存储与清理")
        grid = QFormLayout(actions)
        for key, label in (
            ("open_data", "打开数据目录"),
            ("clear_all_logs", "清理全部日志"),
            ("cleanup_logs", "清理过期日志"),
            ("clear_history", "清理启动历史"),
            ("clear_temporary", "清理临时文件"),
            ("reset_ui", "重置窗口与界面设置"),
            ("delete_all_data", "删除全部 LLMBatDesk 用户数据"),
        ):
            button = QPushButton(label)
            button.setObjectName(f"settings_{key}")
            button.clicked.connect(lambda _checked=False, value=key: self.actionRequested.emit(value))
            grid.addRow(button)
        layout.addWidget(actions)
        layout.addStretch()
        return page

    def _appearance_page(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.theme = QComboBox()
        self.theme.addItem("深色", "dark")
        self.theme.addItem("浅色", "light")
        self.theme.setCurrentIndex(max(0, self.theme.findData(self.settings.theme)))
        form.addRow("主题", self.theme)
        return page

    def _extensions_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        group = QGroupBox("模型库")
        form = QFormLayout(group)
        self.model_library_enabled = QCheckBox("启用模型库")
        self.model_library_enabled.setChecked(
            self.settings.model_library_enabled
        )
        self.model_library_enabled.setToolTip(
            "可选的本地 GGUF 索引。默认关闭；不会自动扫描、加载模型或代理推理请求。"
        )
        form.addRow(self.model_library_enabled)
        self.model_library_allow_running_scan = QCheckBox(
            "允许模型运行时手动扫描"
        )
        self.model_library_allow_running_scan.setChecked(
            self.settings.model_library_allow_running_scan
        )
        self.model_library_allow_running_scan.setToolTip(
            "高级选项。即使启用也只响应手动扫描；磁盘活动可能影响模型加载或推理。"
        )
        form.addRow(self.model_library_allow_running_scan)
        notice = QLabel(
            "模型库仅扫描用户明确添加的目录。启用后，数据库和扫描器仍会等到首次打开"
            "“模型库”标签页时才初始化。关闭扩展不会删除索引、标签或备注。"
        )
        notice.setWordWrap(True)
        form.addRow(notice)
        self.model_library_enabled.toggled.connect(
            self.model_library_allow_running_scan.setEnabled
        )
        self.model_library_allow_running_scan.setEnabled(
            self.model_library_enabled.isChecked()
        )
        layout.addWidget(group)
        layout.addStretch()
        return page

    def update_storage_stats(self, stats: StorageStats) -> None:
        self.storage_summary.setText(
            f"当前总大小：{_format_size(stats.total_size)}\n"
            f"数据库：{_format_size(stats.database_size)}\n"
            f"日志：{stats.log_count} 个，共 {_format_size(stats.log_size)}\n"
            f"启动历史：{stats.history_count} 条\n"
            f"临时文件：{_format_size(stats.temporary_size)}"
        )

    def _save(self) -> None:
        self.settings.editor_mode = self.editor_mode.currentData()
        self.settings.editor_path = self.editor_path.text().strip()
        if (
            self.settings.editor_mode == EditorMode.CUSTOM
            and resolve_editor_executable(self.settings.editor_path) is None
        ):
            self._set_editor_error(
                "无法保存：指定编辑器不存在或不是有效的 .exe 可执行文件。"
            )
            self.tabs.setCurrentIndex(0)
            return
        self.settings.launch_mode = self.launch_mode.currentData()
        self.settings.launch_confirmation_mode = self.confirm_mode.currentData()
        self.settings.api_startup_timeout_seconds = float(self.api_timeout.value())
        self.settings.log_retention_days = (
            None if self.retention_unlimited.isChecked() else self.retention.value()
        )
        self.settings.log_max_total_mb = (
            None if self.max_mb_unlimited.isChecked() else self.max_mb.value()
        )
        self.settings.log_max_count = (
            None if self.max_count_unlimited.isChecked() else self.max_count.value()
        )
        self.settings.terminal_font_family = self.terminal_font_family.currentText()
        self.settings.terminal_font_size = self.terminal_font_size.value()
        self.settings.terminal_line_spacing_percent = (
            self.terminal_line_spacing.value()
        )
        self.settings.theme = self.theme.currentData()
        self.settings.model_library_enabled = (
            self.model_library_enabled.isChecked()
        )
        self.settings.model_library_allow_running_scan = (
            self.model_library_allow_running_scan.isChecked()
        )
        self.accept()


class DeleteUserDataDialog(QDialog):
    def __init__(self, data_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("删除全部 LLMBatDesk 用户数据")
        self.resize(620, 320)
        self.setMinimumSize(520, 280)
        layout = QVBoxLayout(self)
        warning = QLabel(
            f"将删除以下目录中的配置、数据库、日志、元数据和临时文件：\n{data_dir}\n\n"
            "不会删除当前运行的 EXE。此操作不可撤销。\n"
            "请输入“删除全部数据”继续："
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.confirmation = QLineEdit()
        layout.addWidget(self.confirmation)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("永久删除")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.confirmation.textChanged.connect(
            lambda value: buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                value == "删除全部数据"
            )
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
