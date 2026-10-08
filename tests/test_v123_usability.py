from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from llmbatdesk.discovery import ScriptScanner
from llmbatdesk.domain.models import (
    ApiReadinessStatus, EditorMode, LaunchConfirmationMode, ManagedLaunch,
    ProcessIdentity, RuntimeState,
)
from llmbatdesk.qt.dialogs import AlternatePortDialog, SettingsDialog
from llmbatdesk.qt.main_window import MainWindow
from llmbatdesk.qt.models import api_display_state, runtime_display_state
from llmbatdesk.qt.terminal_log import TerminalLogView, default_terminal_font_family
from llmbatdesk.qt.theme import DARK_QSS, LIGHT_QSS, apply_theme
from llmbatdesk.runtime.override import generate_port_override
from llmbatdesk.services import ApplicationService
from llmbatdesk.settings import AppSettings, SettingsStore
from llmbatdesk.storage import MetadataStore


def service(tmp_path: Path) -> ApplicationService:
    data = tmp_path / "data"
    return ApplicationService(
        data_dir=data,
        settings_store=SettingsStore(data),
        metadata_store=MetadataStore(data / "db.sqlite"),
    )


def runnable_record(
    tmp_path: Path, *, prefix: str = "", suffix: str = "", port: int = 8080
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    script = tmp_path / "run.bat"
    executable.write_bytes(b"fake")
    model.write_bytes(b"fake")
    script.write_text(
        prefix
        + f'"{executable}" --model "{model}" --port {port}\n'
        + suffix,
        encoding="utf-8",
    )
    return ScriptScanner().scan([], [script])[0]


def make_window(qtbot, tmp_path: Path, record=None) -> MainWindow:
    app_service = service(tmp_path)
    window = MainWindow(app_service, auto_scan=False)
    window.runtime_timer.stop()
    window.log_timer.stop()
    qtbot.addWidget(window)
    window.show()
    if record:
        app_service.records[record.fingerprint.canonical_path] = record
        window.bind_records([record])
    return window


def managed(record, state: RuntimeState, *, verified: bool = True) -> ManagedLaunch:
    server = ProcessIdentity(pid=20, create_time=20, executable=str(record.parsed.executable))
    return ManagedLaunch(
        launch_id=f"launch-{state.name}", script_path=str(record.parsed.path),
        script_hash=record.fingerprint.sha256,
        identity=ProcessIdentity(pid=10, create_time=10, executable="cmd.exe"),
        server_identity=server, child_identities=[server],
        configured_port=8080, actual_port=8080, log_path="x.log",
        state=state, verified=verified, started_at=datetime.now(),
    )


def test_legacy_editor_path_migrates_to_custom_mode() -> None:
    settings = AppSettings(editor_path="notepad.exe")
    assert settings.editor_mode == EditorMode.CUSTOM
    assert AppSettings().editor_mode == EditorMode.SYSTEM_DEFAULT


def test_system_default_editor_mode_uses_file_association(tmp_path: Path, monkeypatch) -> None:
    app = service(tmp_path)
    script = tmp_path / "测试 脚本.bat"
    script.write_text("@echo off", encoding="utf-8")
    opened: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        "llmbatdesk.services.os.startfile",
        lambda path, verb: opened.append((path, verb)),
    )
    app.settings.editor_mode = EditorMode.SYSTEM_DEFAULT
    app.open_script(script)
    assert opened == [(script, "edit")]


def test_custom_editor_path_with_spaces_and_chinese_is_safe(
    tmp_path: Path, monkeypatch
) -> None:
    app = service(tmp_path)
    editor = tmp_path / "中文 编辑器" / "Editor.exe"
    editor.parent.mkdir()
    editor.write_bytes(b"fake")
    script = tmp_path / "模型 启动.bat"
    script.write_text("@echo off", encoding="utf-8")
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or object(),
    )
    app.settings.editor_mode = EditorMode.CUSTOM
    app.settings.editor_path = str(editor)
    app.open_script(script)
    command, kwargs = calls[0]
    assert command == [str(editor.resolve()), str(script.resolve())]
    assert kwargs["shell"] is False


def test_missing_custom_editor_is_rejected(tmp_path: Path) -> None:
    app = service(tmp_path)
    script = tmp_path / "x.bat"
    script.write_text("@echo off", encoding="utf-8")
    app.settings.editor_mode = EditorMode.CUSTOM
    app.settings.editor_path = str(tmp_path / "missing.exe")
    with pytest.raises(FileNotFoundError):
        app.open_script(script)


def test_editor_restore_default_and_read_only_path(qtbot, tmp_path: Path) -> None:
    settings = AppSettings(
        editor_mode=EditorMode.CUSTOM, editor_path=str(tmp_path / "Editor.exe")
    )
    dialog = SettingsDialog(settings, tmp_path)
    qtbot.addWidget(dialog)
    assert dialog.editor_path.isReadOnly()
    dialog.editor_reset.click()
    assert dialog.editor_mode.currentData() == EditorMode.SYSTEM_DEFAULT
    assert dialog.editor_path.text() == ""
    assert dialog.editor_error.isHidden()
    assert dialog.editor_error.text() == ""


def test_editor_validation_banner_hidden_in_system_default_mode(
    qtbot, tmp_path: Path
) -> None:
    dialog = SettingsDialog(
        AppSettings(editor_mode=EditorMode.SYSTEM_DEFAULT, editor_path=""),
        tmp_path,
    )
    qtbot.addWidget(dialog)

    assert dialog.editor_mode.currentData() == EditorMode.SYSTEM_DEFAULT
    assert dialog.editor_error.isHidden()
    assert dialog.editor_error.text() == ""


def test_editor_validation_banner_hidden_for_valid_custom_editor(
    qtbot, tmp_path: Path
) -> None:
    editor = tmp_path / "中文 编辑器" / "Editor.exe"
    editor.parent.mkdir()
    editor.write_bytes(b"fake")
    dialog = SettingsDialog(
        AppSettings(editor_mode=EditorMode.CUSTOM, editor_path=str(editor)),
        tmp_path,
    )
    qtbot.addWidget(dialog)

    assert dialog.editor_error.isHidden()
    assert dialog.editor_error.text() == ""


def test_editor_validation_banner_updates_for_missing_custom_editor(
    qtbot, tmp_path: Path
) -> None:
    dialog = SettingsDialog(AppSettings(), tmp_path)
    qtbot.addWidget(dialog)
    dialog.editor_mode.setCurrentIndex(
        dialog.editor_mode.findData(EditorMode.CUSTOM)
    )

    assert not dialog.editor_error.isHidden()
    assert "尚未选择指定编辑器" in dialog.editor_error.text()

    dialog.editor_path.setText(str(tmp_path / "missing.exe"))
    assert not dialog.editor_error.isHidden()
    assert "不存在或不是有效的可执行文件" in dialog.editor_error.text()


def test_terminal_font_persistence_clamping_and_reset(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    settings = AppSettings(
        terminal_font_family="Consolas",
        terminal_font_size=99,
        terminal_line_spacing_percent=10,
    )
    assert settings.terminal_font_size == 24
    assert settings.terminal_line_spacing_percent == 80
    store.save(settings)
    loaded = store.load()
    assert loaded.terminal_font_size == 24
    assert loaded.terminal_line_spacing_percent == 80


def test_terminal_font_applies_immediately_and_resets(qtbot) -> None:
    view = TerminalLogView()
    qtbot.addWidget(view)
    view.configure_font(default_terminal_font_family(), 17, 135)
    assert view.font().pointSize() == 17
    assert view.line_spacing_percent == 135
    view.change_font_size(20)
    assert view.font().pointSize() == 24
    view.change_font_size(-100)
    assert view.font().pointSize() == 8
    view.reset_font_size()
    assert view.font().pointSize() == 17


def test_ctrl_wheel_changes_only_terminal_font(qtbot) -> None:
    view = TerminalLogView()
    qtbot.addWidget(view)
    view.configure_font("", 11, 100)

    class Event:
        accepted = False

        @staticmethod
        def modifiers():
            from PySide6.QtCore import Qt
            return Qt.KeyboardModifier.ControlModifier

        @staticmethod
        def angleDelta():
            return QPoint(0, 120)

        def accept(self):
            self.accepted = True

    event = Event()
    view.wheelEvent(event)
    assert view.font().pointSize() == 12
    assert event.accepted


def test_trailing_pause_is_harmless_after_one_time_trust(tmp_path: Path) -> None:
    app = service(tmp_path)
    record = runnable_record(tmp_path, suffix="pause\n")
    assert record.parsed.harmless_trailing_pause
    assert "pause" not in record.parsed.interactive_commands
    assert app.should_confirm_launch(record)
    app.trust(record)
    assert not app.should_confirm_launch(record)


@pytest.mark.parametrize(
    ("prefix", "suffix", "command"),
    [
        ("pause\n", "", "pause"),
        ("set /p ANSWER=继续吗\n", "", "set /p"),
        ("choice /c YN\n", "", "choice"),
    ],
)
def test_meaningful_interaction_still_confirms(
    tmp_path: Path, prefix: str, suffix: str, command: str
) -> None:
    app = service(tmp_path)
    record = runnable_record(tmp_path, prefix=prefix, suffix=suffix)
    assert command in record.parsed.interactive_commands
    app.trust(record)
    reasons = app.safety_confirmation_reasons(record)
    assert any("需要输入的交互命令" in reason for reason in reasons)
    assert app.should_confirm_launch(record)


def test_confirmation_reasons_distinguish_content_and_working_directory(
    tmp_path: Path,
) -> None:
    app = service(tmp_path)
    record = runnable_record(tmp_path)
    assert app.safety_confirmation_reasons(record)[0] == "新脚本，尚未信任"
    app.trust(record)
    record.parsed.path.write_text(record.parsed.raw_text + "\nrem changed", encoding="utf-8")
    changed = ScriptScanner().scan([], [record.parsed.path])[0]
    assert "脚本内容已变化" in app.safety_confirmation_reasons(changed)
    assert "工作目录已变化" in app.safety_confirmation_reasons(
        record, working_directory=tmp_path / "other"
    )


def test_temporary_override_remains_mandatory_after_trust(tmp_path: Path) -> None:
    app = service(tmp_path)
    record = runnable_record(tmp_path)
    app.trust(record)
    override = app.prepare_override(record, 8081)
    try:
        assert "临时端口覆盖" in app.safety_confirmation_reasons(
            record, override=override
        )
        assert app.should_confirm_launch(record, override=override)
    finally:
        override.path.unlink(missing_ok=True)


def test_alternate_port_auto_generates_without_opening_diff(qtbot, tmp_path: Path) -> None:
    record = runnable_record(tmp_path)
    original = record.parsed.path.read_bytes()
    dialog = AlternatePortDialog(
        record, "目标", 8081, lambda _port: None, lambda _start: 8081,
        lambda item, port: generate_port_override(
            item.parsed, original, port, tmp_path / "temporary"
        ),
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.override is not None, timeout=3000)
    assert dialog.launch_button.isEnabled()
    assert dialog.generate_button.text() == "查看完整差异"
    assert not dialog.diff.isVisible()
    assert "原始 BAT/CMD 保持逐字节不变" in dialog.patch_summary.text()
    assert record.parsed.path.read_bytes() == original
    dialog.reject()


def test_alternate_port_change_ignores_stale_generation(qtbot, tmp_path: Path) -> None:
    record = runnable_record(tmp_path)
    original = record.parsed.path.read_bytes()

    def prepare(item, port):
        if port == 8081:
            time.sleep(0.08)
        return generate_port_override(
            item.parsed, original, port, tmp_path / "temporary"
        )

    dialog = AlternatePortDialog(
        record, "目标", 8081, lambda _port: None, lambda _start: 8082, prepare
    )
    qtbot.addWidget(dialog)
    dialog.port_spin.setValue(8082)
    qtbot.waitUntil(
        lambda: dialog.override is not None and dialog.override.new_port == 8082,
        timeout=4000,
    )
    assert "8082" in dialog.patch_summary.text()
    assert record.parsed.path.read_bytes() == original
    dialog.reject()


def test_alternate_port_generation_failure_is_clear(qtbot, tmp_path: Path) -> None:
    record = runnable_record(tmp_path)
    dialog = AlternatePortDialog(
        record, "目标", 8081, lambda _port: None, lambda _start: 8081,
        lambda _record, _port: (_ for _ in ()).throw(ValueError("不安全补丁")),
    )
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: "不安全补丁" in dialog.patch_summary.text(), timeout=3000)
    assert not dialog.launch_button.isEnabled()
    assert "生成失败" in dialog.patch_summary.text()


def test_using_port_while_generation_runs_continues_when_safe(
    qtbot, tmp_path: Path
) -> None:
    record = runnable_record(tmp_path)
    original = record.parsed.path.read_bytes()

    def prepare(item, port):
        time.sleep(0.05)
        return generate_port_override(
            item.parsed, original, port, tmp_path / "temporary"
        )

    dialog = AlternatePortDialog(
        record, "目标", 8081, lambda _port: None, lambda _start: 8081, prepare
    )
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._use_selected_port()
    qtbot.waitUntil(
        lambda: dialog.result() == dialog.DialogCode.Accepted, timeout=3000
    )
    assert dialog.override is not None
    assert dialog.override.new_port == 8081
    assert record.parsed.path.read_bytes() == original


@pytest.mark.parametrize(
    ("state", "api_status", "api_ready", "runtime_text", "api_text"),
    [
        (RuntimeState.API_READY, ApiReadinessStatus.READY, True, "运行中", "API 已就绪"),
        (RuntimeState.PORT_LISTENING_API_NOT_READY, ApiReadinessStatus.PENDING, False, "运行中", "检查中"),
        (RuntimeState.AUTH_REQUIRED, ApiReadinessStatus.AUTH_REQUIRED, False, "运行中", "需要认证"),
        (RuntimeState.STARTING, ApiReadinessStatus.NOT_CHECKED, False, "正在启动", "未检查"),
        (RuntimeState.FAILED, ApiReadinessStatus.NOT_CHECKED, False, "启动失败", "不适用"),
        (RuntimeState.STOPPING, ApiReadinessStatus.NOT_CHECKED, False, "正在停止", "不适用"),
    ],
)
def test_runtime_and_api_columns_are_independent(
    state, api_status, api_ready, runtime_text, api_text
) -> None:
    launch = ManagedLaunch(
        launch_id="state", script_path="x", script_hash="h",
        identity=ProcessIdentity(pid=1, create_time=1), log_path="x",
        state=state, api_status=api_status, api_ready=api_ready,
    )
    assert runtime_display_state(state) == runtime_text
    assert api_display_state(launch) == api_text


def test_action_button_semantics_for_idle_starting_running_stopping_failed(
    qtbot, tmp_path: Path
) -> None:
    record = runnable_record(tmp_path)
    window = make_window(qtbot, tmp_path / "window", record)
    assert window.actions["start"].isEnabled()
    assert window.actions["start"].property("semanticRole") == "start"
    assert not window.actions["stop"].isEnabled()

    starting = managed(record, RuntimeState.STARTING, verified=False)
    window.service.launches = {starting.launch_id: starting}
    window._update_actions()
    assert window.actions["start"].text() == "启动中……"
    assert not window.actions["restart"].isEnabled()

    running = managed(record, RuntimeState.API_READY)
    window.service.launches = {running.launch_id: running}
    window._update_actions()
    assert not window.actions["start"].isEnabled()
    assert window.actions["stop"].property("semanticRole") == "danger"
    assert window.actions["restart"].property("semanticRole") == "warning"
    assert window.actions["api"].property("semanticRole") == "info"

    stopping = managed(record, RuntimeState.STOPPING)
    window.service.launches = {stopping.launch_id: stopping}
    window._update_actions()
    assert window.actions["stop"].text() == "停止中……"
    assert not window.actions["stop"].isEnabled()
    assert not window.actions["restart"].isEnabled()

    failed = managed(record, RuntimeState.FAILED, verified=False)
    window.service.launches = {failed.launch_id: failed}
    window._update_actions()
    assert window.actions["start"].text() == "重试"
    assert window.actions["start"].property("semanticRole") == "start"


def test_semantic_action_styles_exist_in_both_themes(qapp) -> None:
    for stylesheet in (DARK_QSS, LIGHT_QSS):
        assert 'semanticRole="start"' in stylesheet
        assert 'semanticRole="danger"' in stylesheet
        assert 'semanticRole="warning"' in stylesheet
        assert 'semanticRole="info"' in stylesheet
    apply_theme(qapp, "dark")
    apply_theme(qapp, "light")
