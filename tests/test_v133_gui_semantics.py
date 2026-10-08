from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QBrush, QPalette
from PySide6.QtWidgets import QApplication

from llmbatdesk.domain.models import LaunchHistoryItem
from llmbatdesk.extensions.model_library.models import (
    ModelFileRole, ModelRecord, ModelStatus, ReferenceRole, ReferenceState,
    ScanSummary, ScriptReference,
)
from llmbatdesk.extensions.model_library.page import ModelLibraryPage
from llmbatdesk.extensions.model_library.qt_models import (
    MODEL_FILE_ROLE_ROLE, MODEL_FILE_ROLE_MARKERS, ModelFilterProxy,
    ModelTableModel,
)
from llmbatdesk.extensions.model_library.service import ModelLibraryService
from llmbatdesk.extensions.model_library.storage import ModelLibraryStore
from llmbatdesk.qt.main_window import MainWindow
from llmbatdesk.qt.theme import MODEL_ROLE_COLORS, apply_theme
from llmbatdesk.services import ApplicationService
from llmbatdesk.settings import SettingsStore
from llmbatdesk.storage import MetadataStore


def _service(tmp_path: Path) -> ApplicationService:
    data = tmp_path / "data"
    return ApplicationService(
        data_dir=data,
        settings_store=SettingsStore(data),
        metadata_store=MetadataStore(data / "core.sqlite"),
    )


def _window(qtbot, tmp_path: Path) -> MainWindow:
    window = MainWindow(_service(tmp_path), auto_scan=False)
    window.runtime_timer.stop()
    window.log_timer.stop()
    qtbot.addWidget(window)
    window.show()
    return window


def _record(
    model_id: int, path: Path, role: ModelFileRole,
    *, status: ModelStatus = ModelStatus.AVAILABLE,
) -> ModelRecord:
    return ModelRecord(
        model_id=model_id, root_id=1, canonical_path=str(path.resolve()),
        filename=path.name, name=path.stem, size=1, mtime_ns=1,
        status=status, model_role=role, metadata_role=role,
        role_provenance="test",
    )


@pytest.mark.parametrize(
    ("role", "marker"),
    [
        (ModelFileRole.MMPROJ, "〔投影〕"),
        (ModelFileRole.DRAFT, "〔草稿〕"),
        (ModelFileRole.ADAPTER, "〔适配〕"),
        (ModelFileRole.CONTROL, "〔控制〕"),
        (ModelFileRole.EMBEDDING, "〔嵌入〕"),
        (ModelFileRole.AUXILIARY, "〔附属〕"),
        (ModelFileRole.UNKNOWN, "〔未知〕"),
    ],
)
def test_model_role_has_textual_and_semantic_indicator(
    qapp, tmp_path: Path, role: ModelFileRole, marker: str
) -> None:
    model = ModelTableModel()
    model.set_records([_record(1, tmp_path / f"{role}.gguf", role)])
    index = model.index(0, 0)
    assert index.data().startswith(marker)
    assert index.data(MODEL_FILE_ROLE_ROLE) == role.value
    assert role.value in MODEL_ROLE_COLORS["dark"]
    assert isinstance(index.data(Qt.ItemDataRole.ForegroundRole), QBrush)
    assert role.value.replace("_", "") or index.data(
        Qt.ItemDataRole.AccessibleTextRole
    )


def test_primary_role_keeps_normal_appearance(tmp_path: Path) -> None:
    model = ModelTableModel()
    model.set_records([
        _record(1, tmp_path / "primary.gguf", ModelFileRole.PRIMARY)
    ])
    index = model.index(0, 0)
    assert index.data() == "primary"
    assert index.data(Qt.ItemDataRole.ForegroundRole) is None
    assert index.data(MODEL_FILE_ROLE_ROLE) == "primary"


def test_role_colors_follow_dark_and_light_theme(qapp, tmp_path: Path) -> None:
    model = ModelTableModel()
    model.set_records([
        _record(1, tmp_path / "mmproj.gguf", ModelFileRole.MMPROJ)
    ])
    index = model.index(0, 0)
    colors = []
    for theme in ("dark", "light"):
        apply_theme(qapp, theme)
        colors.append(index.data(Qt.ItemDataRole.ForegroundRole).color().name())
        assert colors[-1] == MODEL_ROLE_COLORS[theme]["mmproj"]
        qss = qapp.styleSheet()
        assert "QTableView::item:selected" in qss
        assert "QTableView::item:hover" in qss
    assert colors[0] != colors[1]


def test_hidden_auxiliary_removes_role_styled_rows(tmp_path: Path) -> None:
    model = ModelTableModel()
    model.set_records([
        _record(1, tmp_path / "primary.gguf", ModelFileRole.PRIMARY),
        _record(2, tmp_path / "mmproj.gguf", ModelFileRole.MMPROJ),
    ])
    proxy = ModelFilterProxy()
    proxy.setSourceModel(model)
    assert proxy.rowCount() == 1
    assert proxy.index(0, 0).data(MODEL_FILE_ROLE_ROLE) == "primary"
    proxy.set_filters(show_auxiliary=True)
    assert proxy.rowCount() == 2


def test_scan_stop_button_state_machine_and_no_timer(qtbot, tmp_path: Path) -> None:
    page = ModelLibraryPage(tmp_path, [])
    qtbot.addWidget(page)
    timer_count = len(page.findChildren(QTimer))
    assert page.cancel_button.text() == "取消扫描"
    assert not page.cancel_button.isEnabled()
    assert page.cancel_button.property("semanticRole") == "neutral"

    page._set_scan_action_state("scanning")
    assert page.cancel_button.text() == "停止扫描"
    assert page.cancel_button.isEnabled()
    assert page.cancel_button.property("semanticRole") == "danger"

    page._set_scan_action_state("stopping")
    assert page.cancel_button.text() == "正在停止……"
    assert not page.cancel_button.isEnabled()
    assert page.cancel_button.property("semanticRole") == "danger"

    for terminal_status in ("扫描已取消", "扫描完成", "扫描失败"):
        page.scan_status.setText(terminal_status)
        page._set_scan_action_state("idle")
        assert page.cancel_button.text() == "取消扫描"
        assert not page.cancel_button.isEnabled()
        assert page.cancel_button.property("semanticRole") == "neutral"
    assert len(page.findChildren(QTimer)) == timer_count
    page.shutdown()


class _CancelAwareScanner:
    def scan(self, job_id, roots, cancelled, **_kwargs):
        cancelled.wait(5)
        return ScanSummary(job_id=job_id, root_ids=[], cancelled=True)


def test_real_scan_start_and_cancel_updates_button_then_returns_idle(
    qtbot, tmp_path: Path
) -> None:
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    service = ModelLibraryService(
        tmp_path, store=store, scanner=_CancelAwareScanner()
    )
    page = ModelLibraryPage(tmp_path, [], service=service)
    qtbot.addWidget(page)
    page.scan_all()
    qtbot.waitUntil(lambda: page.cancel_button.isEnabled())
    assert page.cancel_button.text() == "停止扫描"
    page.cancel_button.click()
    assert page.cancel_button.text() == "正在停止……"
    assert not page.cancel_button.isEnabled()
    qtbot.waitUntil(lambda: page._thread is None, timeout=10_000)
    assert page.cancel_button.text() == "取消扫描"
    assert not page.cancel_button.isEnabled()
    assert page.scan_status.text() == "扫描已取消"
    page.shutdown()


def _relationship_page(qtbot, tmp_path: Path):
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    folder = tmp_path / "models"
    folder.mkdir()
    root = store.add_root(str(folder.resolve()), "models")
    paths = {
        "p1": folder / "primary-one.gguf",
        "p2": folder / "primary-two.gguf",
        "p3": folder / "primary-empty.gguf",
        "aux": folder / "mmproj.gguf",
    }
    roles = {
        "p1": ModelFileRole.PRIMARY,
        "p2": ModelFileRole.PRIMARY,
        "p3": ModelFileRole.PRIMARY,
        "aux": ModelFileRole.MMPROJ,
    }
    for number, (key, path) in enumerate(paths.items(), 1):
        path.write_bytes(b"x")
        store.upsert_model(_record(number, path, roles[key]))
    references = []
    for number, primary in enumerate(("p1", "p2"), 1):
        script = tmp_path / f"launch-{number}.bat"
        references.extend([
            ScriptReference(
                script_path=str(script), script_name=script.name,
                state=ReferenceState.EXPLICIT,
                model_path=str(paths[primary].resolve()),
                role=ReferenceRole.PRIMARY,
            ),
            ScriptReference(
                script_path=str(script), script_name=script.name,
                state=ReferenceState.EXPLICIT,
                model_path=str(paths["aux"].resolve()),
                role=ReferenceRole.MMPROJ,
            ),
        ])
    store.replace_references(references)
    service = ModelLibraryService(tmp_path, store=store)
    page = ModelLibraryPage(tmp_path, [], service=service)
    # Page initialization synchronizes the supplied empty script set.
    store.replace_references(references)
    page.refresh_models()
    qtbot.addWidget(page)
    return page, paths


def _select_path(page: ModelLibraryPage, path: Path) -> None:
    for row in range(page.model_model.rowCount()):
        record = page.model_model.record_at(row)
        if record and record.canonical_path == str(path.resolve()):
            proxy = page.proxy_model.mapFromSource(page.model_model.index(row, 0))
            assert proxy.isValid()
            page.model_table.selectRow(proxy.row())
            QApplication.processEvents()
            return
    raise AssertionError(f"model not found: {path}")


def test_association_section_collapses_and_reverses_many_to_many(
    qtbot, tmp_path: Path
) -> None:
    page, paths = _relationship_page(qtbot, tmp_path)

    _select_path(page, paths["p3"])
    assert page.component_title.text() == "关联组件（0）"
    assert page.component_empty.text() == "无关联组件"
    assert page.component_empty.isVisibleTo(page)
    assert not page.components.isVisibleTo(page)

    _select_path(page, paths["p1"])
    assert page.component_title.text() == "关联组件（1）"
    assert page.component_model.rowCount() == 1
    assert page.component_model.index(0, 1).data() == "多模态投影"
    assert "明确" in page.component_model.index(0, 4).data()

    page.show_auxiliary.setChecked(True)
    _select_path(page, paths["aux"])
    assert page.component_title.text() == "关联主模型（2）"
    assert page.component_model.rowCount() == 2
    assert page.component_model.headerData(
        0, Qt.Orientation.Horizontal
    ) == "主模型"
    assert all(
        page.component_model.index(row, 1).data() == "主模型"
        for row in range(2)
    )
    page.shutdown()


def test_global_rescan_is_top_level_only_and_invokes_library_scan(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    window = _window(qtbot, tmp_path)
    calls: list[str] = []

    def fake_run_async(function, on_success, on_error=None, on_finished=None):
        calls.append("rescan")
        on_success(function())
        if on_finished:
            on_finished()
        return object()

    monkeypatch.setattr(window.service, "rescan", lambda: [])
    monkeypatch.setattr(window, "_run_async", fake_run_async)
    window.script_rescan_button.click()
    assert calls == ["rescan"]
    assert "rescan" not in window.actions
    assert window.script_rescan_button.text() == "重新扫描脚本库"
    assert "所有已配置脚本目录" in window.script_rescan_button.toolTip()
    assert window.script_rescan_action.shortcut().toString() == "F5"
    assert (
        window.add_root_button.nextInFocusChain()
        is window.add_script_button
    )


def test_global_rescan_busy_state_is_disabled(qtbot, tmp_path: Path) -> None:
    window = _window(qtbot, tmp_path)
    window._script_rescan_busy = True
    window._update_actions()
    assert not window.script_rescan_button.isEnabled()
    assert not window.script_rescan_action.isEnabled()


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_launch_history_has_no_blue_alternating_rows(
    qapp, qtbot, tmp_path: Path, theme: str
) -> None:
    apply_theme(qapp, theme)
    window = _window(qtbot, tmp_path)
    entries = [
        LaunchHistoryItem(
            history_id=f"h-{number}", script_path=f"C:/scripts/{number}.bat",
            state="stopped", timestamp=datetime.now(),
        )
        for number in range(2)
    ]
    window.history_model.set_history(entries, {}, lambda _record: "")
    window.history_page.stack.setCurrentWidget(window.history_table)
    assert not window.history_table.alternatingRowColors()
    for row in range(2):
        assert window.history_model.index(
            row, 0
        ).data(Qt.ItemDataRole.BackgroundRole) is None
    alternate = qapp.palette().color(QPalette.ColorRole.AlternateBase)
    selected = qapp.palette().color(QPalette.ColorRole.Highlight)
    assert alternate != selected
    assert selected.name() == "#3478d4"
    window.history_table.selectRow(1)
    assert window.history_table.selectionModel().selectedRows()[0].row() == 1
    qss = qapp.styleSheet()
    assert "QTableView#LaunchHistoryTable::item:hover" in qss
    assert "QTableView#LaunchHistoryTable::item:selected" in qss
