from __future__ import annotations

from datetime import datetime
import inspect
import os
from pathlib import Path
import subprocess
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from llmbatdesk.discovery import ScriptScanner
from llmbatdesk.domain.models import (
    ManagedLaunch, PortOccupant, ProcessIdentity, RuntimeState,
)
from llmbatdesk.qt.app import (
    APPLICATION_ICON_PATH, PREFERRED_FONTS, apply_application_icon,
    create_application, dpi_diagnostics, select_application_font,
)
from llmbatdesk.qt.dialogs import AlternatePortDialog, ConflictDialog, TextDialog
from llmbatdesk.qt.main_window import MainWindow
from llmbatdesk.qt.theme import apply_theme
from llmbatdesk.services import ApplicationService, LaunchBlockedError
from llmbatdesk.settings import SettingsStore
from llmbatdesk.storage import MetadataStore


def make_service(tmp_path: Path) -> ApplicationService:
    data = tmp_path / "data"
    return ApplicationService(
        data_dir=data,
        settings_store=SettingsStore(data),
        metadata_store=MetadataStore(data / "db.sqlite"),
    )


def make_window(qtbot, tmp_path: Path) -> MainWindow:
    service = make_service(tmp_path)
    apply_theme(QApplication.instance(), "dark")
    window = MainWindow(service, dpi_diagnostics=dpi_diagnostics(QApplication.instance()), auto_scan=False)
    window.runtime_timer.stop()
    window.log_timer.stop()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitExposed(window)
    return window


def copied_record(tmp_path: Path, model_name: str = "model.gguf"):
    script = tmp_path / "copied.bat"
    script.write_text(
        f'"Z:\\missing\\llama-server.exe" --model "Z:\\models\\{model_name}" '
        "--host 0.0.0.0 --port 8080 --ctx-size 32768",
        encoding="utf-8",
    )
    return ScriptScanner().scan([], [script])[0]


def test_high_dpi_policy_font_and_diagnostics(qapp) -> None:
    assert (
        QGuiApplication.highDpiScaleFactorRoundingPolicy()
        == Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    font = select_application_font(qapp)
    assert font.pointSizeF() > 0
    assert font.family() in (*PREFERRED_FONTS, qapp.font().family())
    details = dpi_diagnostics(qapp)
    assert "逻辑 DPI" in details
    assert "设备像素比" in details
    assert "有效字号" in details


def test_entrypoint_configures_dpi_before_qapplication() -> None:
    source = inspect.getsource(create_application)
    assert source.index("configure_high_dpi()") < source.index("QApplication(")


def test_embedded_icon_resource_and_application_icon_load(qapp) -> None:
    assert not QIcon(APPLICATION_ICON_PATH).isNull()
    apply_application_icon(qapp)
    assert not qapp.windowIcon().isNull()


@pytest.mark.parametrize("scale", ["1.25", "1.5", "2.0"])
def test_common_windows_scale_factors_keep_layout_usable(tmp_path: Path, scale: str) -> None:
    environment = os.environ.copy()
    environment.update({
        "QT_QPA_PLATFORM": "offscreen",
        "QT_SCALE_FACTOR": scale,
        "PYTHONPATH": str(Path.cwd() / "src"),
        "LLMBATDESK_DATA_DIR": str(tmp_path / f"scale-{scale}"),
    })
    code = (
        "from llmbatdesk.qt.app import create_application;"
        "from llmbatdesk.qt.main_window import MainWindow;"
        "from llmbatdesk.services import ApplicationService;"
        "a=create_application(['scale-test']);"
        "w=MainWindow(ApplicationService(),auto_scan=False);"
        "w.resize(1280,720);w.show();a.processEvents();"
        "assert w.action_bar.isVisible();"
        "assert w.main_splitter.count()==3;"
        "assert a.font().pointSizeF()>0;"
        "w.close()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(), env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_main_window_splitter_resizable_and_1280_layout(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    window.resize(1280, 720)
    qtbot.wait(20)
    assert window.minimumWidth() <= 1280
    assert window.minimumHeight() <= 720
    assert window.main_splitter.count() == 3
    before = window.main_splitter.sizes()
    window.main_splitter.setSizes([240, 620, 400])
    assert window.main_splitter.sizes() != before
    assert window.action_bar.isVisible()


def test_geometry_and_splitter_state_persist(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    window.resize(1320, 760)
    window.main_splitter.setSizes([260, 680, 380])
    window.detail_tabs.setCurrentIndex(2)
    window.save_ui_state()
    loaded = window.service.settings_store.load()
    assert loaded.qt_geometry
    assert loaded.qt_splitter_state
    assert loaded.qt_selected_tab == 2
    assert loaded.qt_column_widths["active"]


def test_no_selection_has_empty_state_and_persistent_disabled_actions(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    window.bind_records([])
    assert window.detail_stack.currentWidget() is window.detail_empty
    assert "请选择脚本" in window.detail_empty.findChildren(type(window.display_name_label))[0].text()
    assert window.action_bar.isVisible()
    assert not window.actions["start"].isEnabled()
    assert not window.actions["stop"].isEnabled()
    assert not window.actions["restart"].isEnabled()
    assert not window.actions["open_script"].isEnabled()
    assert "选择脚本" in window.actions["start"].toolTip()
    assert set(window.actions) == {
        "start", "stop", "restart", "api", "copy_api",
        "open_api", "open_script", "open_folder", "open_log",
        "remove_script",
    }
    assert not window.actions["remove_script"].isEnabled()
    assert window.script_rescan_button.text() == "重新扫描脚本库"
    assert window.actions["api"].text() == "重新检查 API"
    assert not window.actions["open_api"].isEnabled()


def test_selecting_script_updates_details_and_highlight(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    record = copied_record(tmp_path, "示例模型.gguf")
    window.service.records[record.fingerprint.canonical_path] = record
    window.bind_records([record])
    assert window.selected_record == record
    assert window.library_tree.currentIndex().isValid()
    assert window.library_tree.selectionModel().selectedRows()
    assert window.detail_stack.currentIndex() == 1
    assert window.display_name_label.text() == "copied"
    assert window.copy_fields["model"].value == "示例模型.gguf"
    assert "可执行文件不存在" in window.warnings_text.toPlainText()


def test_rescan_binding_preserves_selection_and_delete_clears(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    first = copied_record(tmp_path)
    second_path = tmp_path / "second.cmd"
    second_path.write_text("ollama serve", encoding="utf-8")
    second = ScriptScanner().scan([], [second_path])[0]
    window.service.records = {
        first.fingerprint.canonical_path: first,
        second.fingerprint.canonical_path: second,
    }
    window.bind_records([first, second])
    window.select_record(second)
    window.bind_records([first, second])
    assert window.selected_record.fingerprint.canonical_path == second.fingerprint.canonical_path
    window.service.records = {first.fingerprint.canonical_path: first}
    window.bind_records([])
    assert window.selected_record is None
    assert window.detail_stack.currentWidget() is window.detail_empty


def test_filter_hiding_selected_script_clears_stale_details(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    record = copied_record(tmp_path)
    window.service.records[record.fingerprint.canonical_path] = record
    window.bind_records([record])
    window.search.setText("definitely-not-present")
    assert window.selected_record is None
    assert window.detail_stack.currentWidget() is window.detail_empty


def test_missing_resources_disable_start_but_keep_open_and_copy(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    record = copied_record(tmp_path)
    window.service.records[record.fingerprint.canonical_path] = record
    window.bind_records([record])
    assert not window.actions["start"].isEnabled()
    assert not window.actions["stop"].isEnabled()
    assert not window.actions["restart"].isEnabled()
    assert window.actions["open_script"].isEnabled()
    assert window.actions["open_folder"].isEnabled()
    assert window.copy_fields["model_path"].copy_button.isEnabled()


def test_empty_copy_disabled_and_exact_long_values_copy_with_feedback(
    qtbot, tmp_path: Path, qapp
) -> None:
    window = make_window(qtbot, tmp_path)
    empty = window.copy_fields["actual_port"]
    assert not empty.copy_button.isEnabled()
    assert "没有可复制" in empty.copy_button.toolTip()
    record = copied_record(tmp_path, "very-long-model-name.gguf")
    window.service.records[record.fingerprint.canonical_path] = record
    window.bind_records([record])
    field = window.copy_fields["model_path"]
    full_value = record.parsed.model_path
    field.resize(120, field.height())
    qtbot.mouseClick(field.copy_button, Qt.MouseButton.LeftButton)
    assert qapp.clipboard().text() == full_value
    assert field.copy_button.text() == "已复制"
    assert "已复制" in window.statusBar().currentMessage()


def test_model_name_keyboard_copy_uses_full_value(qtbot, tmp_path: Path, qapp) -> None:
    window = make_window(qtbot, tmp_path)
    record = copied_record(tmp_path, "完整模型名称.gguf")
    window.service.records[record.fingerprint.canonical_path] = record
    window.bind_records([record])
    window.copy_fields["model"].copy()
    assert qapp.clipboard().text() == "完整模型名称.gguf"


def test_parse_only_and_stale_never_enter_active_model(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    record = copied_record(tmp_path)
    window.service.records[record.fingerprint.canonical_path] = record
    stale = ManagedLaunch(
        launch_id="stale", script_path=str(record.parsed.path), script_hash="h",
        identity=ProcessIdentity(pid=99, create_time=1), configured_port=8080,
        actual_port=8080, log_path="x.log", state=RuntimeState.STALE_RECORD,
        failure_reason="PID 不存在",
    )
    window.service.launches["stale"] = stale
    window._render_runtime_models()
    assert window.active_model.rowCount() == 0
    assert window.history_model.rowCount() == 1
    assert window.active_page.stack.currentIndex() == 0


def test_exactly_one_active_row_per_verified_launch_id(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    record = copied_record(tmp_path)
    window.service.records[record.fingerprint.canonical_path] = record
    server = ProcessIdentity(pid=20, create_time=20, executable="llama-server.exe")
    launch = ManagedLaunch(
        launch_id="one", script_path=str(record.parsed.path), script_hash="h",
        identity=ProcessIdentity(pid=10, create_time=10, executable="cmd.exe"),
        server_identity=server, child_identities=[server], configured_port=8080,
        actual_port=8080, log_path="x.log",
        state=RuntimeState.PORT_LISTENING_API_NOT_READY, verified=True,
        last_verified_at=datetime.now(),
    )
    window.service.launches["one"] = launch
    window._render_runtime_models()
    window._render_runtime_models()
    assert window.active_model.rowCount() == 1
    assert window.active_model.data(window.active_model.index(0, 3)) == 8080
    window.service.launches.clear()


def test_actual_runtime_api_address_is_copied(qtbot, tmp_path: Path, qapp) -> None:
    window = make_window(qtbot, tmp_path)
    record = copied_record(tmp_path)
    window.service.records[record.fingerprint.canonical_path] = record
    server = ProcessIdentity(pid=20, create_time=20, executable="llama-server.exe")
    launch = ManagedLaunch(
        launch_id="actual-port", script_path=str(record.parsed.path), script_hash="h",
        identity=ProcessIdentity(pid=10, create_time=10, executable="cmd.exe"),
        server_identity=server, child_identities=[server], configured_port=8080,
        actual_port=8081, log_path="x.log", state=RuntimeState.API_READY,
        verified=True, api_ready=True, last_verified_at=datetime.now(),
    )
    window.service.launches[launch.launch_id] = launch
    window.bind_records([record])
    window.copy_api()
    assert qapp.clipboard().text() == "http://127.0.0.1:8081/v1"
    window.service.launches.clear()


def test_blocked_missing_resource_attempt_appears_only_in_history(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    record = copied_record(tmp_path)
    window.service.records[record.fingerprint.canonical_path] = record
    window.service.trust(record)
    with pytest.raises(LaunchBlockedError):
        window.service.launch(record)
    window._render_runtime_models()
    assert window.active_model.rowCount() == 0
    assert window.history_model.rowCount() == 1
    assert window.history_model.data(window.history_model.index(0, 3)) == "启动已阻止"


def test_latest_error_copy_field_disables_empty_and_copies_exact_line(
    qtbot, tmp_path: Path, qapp
) -> None:
    window = make_window(qtbot, tmp_path)
    window._set_log_content("normal output\nCUDA error: out of memory\n")
    assert window.latest_error_field.copy_button.isEnabled()
    window.latest_error_field.copy()
    assert qapp.clipboard().text() == "CUDA error: out of memory"
    window._set_log_content("normal output only\n")
    assert not window.latest_error_field.copy_button.isEnabled()


def test_rich_text_dialog_is_resizable_and_scrollable(qtbot) -> None:
    dialog = TextDialog("详情", "line\n" * 200)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.minimumWidth() >= 500
    assert dialog.maximumWidth() > dialog.minimumWidth()
    assert dialog.maximumHeight() > dialog.minimumHeight()
    assert dialog.editor.verticalScrollBar() is not None
    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert "关闭" in labels
    assert "Close" not in labels


def test_help_menu_has_localized_actions_and_content(qtbot, tmp_path: Path) -> None:
    window = make_window(qtbot, tmp_path)
    expected = {
        "help_usage": "使用说明",
        "help_faq": "常见问题",
        "help_security": "安全机制与风险提示",
        "help_dpi": "DPI 与字体诊断",
        "help_about": "关于 LLMBatDesk",
    }
    actions = {
        action.objectName(): action.text()
        for action in window.findChildren(type(window.theme_action))
        if action.objectName().startswith("help_")
    }
    assert actions == expected


@pytest.mark.parametrize(
    ("value", "localized"),
    [(99, "99"), ("auto", "自动"), ("all", "全部"), (None, "未确定")],
)
def test_gpu_layers_localization(value, localized) -> None:
    assert MainWindow._localized_gpu_layers(value) == localized


def test_managed_conflict_dialog_has_exact_required_choices(qtbot, tmp_path: Path) -> None:
    record = copied_record(tmp_path)
    server = ProcessIdentity(pid=20, create_time=20, executable="llama-server.exe")
    launch = ManagedLaunch(
        launch_id="managed", script_path=str(record.parsed.path), script_hash="h",
        identity=ProcessIdentity(pid=10, create_time=10), server_identity=server,
        actual_port=8080, configured_port=8080, log_path="x.log",
        state=RuntimeState.API_READY, verified=True,
    )
    dialog = ConflictDialog(
        record, PortOccupant(port=8080, pid=20, managed_launch_id="managed"),
        launch, record, "目标", "当前", None,
    )
    qtbot.addWidget(dialog)
    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert labels == {
        "停止当前托管服务器并启动目标脚本",
        "仅停止当前托管服务器",
        "在其他端口启动目标脚本",
        "取消",
    }
    assert dialog.minimumWidth() >= 500


def test_unmanaged_conflict_never_offers_stop(qtbot, tmp_path: Path) -> None:
    record = copied_record(tmp_path)
    dialog = ConflictDialog(
        record, PortOccupant(port=8080, pid=99), None, None, "目标", parent=None
    )
    qtbot.addWidget(dialog)
    labels = {button.text() for button in dialog.findChildren(QPushButton)}
    assert not any("停止" in label for label in labels)
    assert labels == {"在其他端口启动目标脚本", "查看进程信息", "取消"}


def test_alternate_port_dialog_availability_and_diff_scrollable(qtbot, tmp_path: Path) -> None:
    executable, model, script = (
        tmp_path / "llama-server.exe", tmp_path / "model.gguf", tmp_path / "x.bat"
    )
    executable.write_bytes(b"fake")
    model.write_bytes(b"fake")
    script.write_text(
        f'"{executable}" --model "{model}" --port 8080\r\nrem keep 8080\r\n',
        encoding="utf-8", newline="",
    )
    record = ScriptScanner().scan([], [script])[0]
    service = make_service(tmp_path)
    dialog = AlternatePortDialog(
        record, "目标", 8081, lambda _port: None, lambda _start: 8081,
        service.prepare_override,
    )
    qtbot.addWidget(dialog)
    assert dialog.check_availability()
    dialog.generate()
    assert dialog.override is not None
    assert dialog.launch_button.isEnabled()
    assert "8081" in dialog.diff.toPlainText()
    assert dialog.diff.verticalScrollBar() is not None
    assert script.read_text(encoding="utf-8").count("8080") == 2
    dialog.reject()
