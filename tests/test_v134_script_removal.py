from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog

from llmbatdesk.discovery import ScriptScanner
from llmbatdesk.domain.models import (
    IgnoredScriptRecord, ManagedLaunch, Metadata, OperationEvent,
    ProcessIdentity, RuntimeState, ScriptRemovalMode,
)
from llmbatdesk.hashing import fingerprint
from llmbatdesk.qt.dialogs import IgnoredScriptsDialog, ScriptRemovalDialog
from llmbatdesk.qt.main_window import MainWindow
from llmbatdesk.qt.theme import apply_theme
from llmbatdesk.services import (
    ApplicationService, LaunchInProgressError,
)
from llmbatdesk.settings import SettingsStore
from llmbatdesk.storage import MetadataStore


class FakeRecycleBin:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.paths: list[Path] = []

    def move_to_trash(self, path: Path) -> None:
        self.paths.append(path)
        if self.error:
            raise self.error


def _service(
    tmp_path: Path, *, recycle: FakeRecycleBin | None = None
) -> ApplicationService:
    data = tmp_path / "data"
    return ApplicationService(
        data_dir=data,
        settings_store=SettingsStore(data),
        metadata_store=MetadataStore(data / "core.sqlite"),
        recycle_bin=recycle or FakeRecycleBin(),
    )


def _script(path: Path, text: str = "@echo off\nrem synthetic\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _individual(app: ApplicationService, path: Path):
    return app.add_individual(_script(path))


def _launch(record, state: RuntimeState) -> ManagedLaunch:
    return ManagedLaunch(
        launch_id=f"launch-{state.name}",
        script_path=str(record.parsed.path),
        script_hash=record.fingerprint.sha256,
        identity=ProcessIdentity(
            pid=10, create_time=10, executable="cmd.exe"
        ),
        configured_port=None,
        actual_port=None,
        log_path="history.log",
        state=state,
        verified=state not in {
            RuntimeState.STARTING, RuntimeState.DETACHED_UNVERIFIED
        },
        started_at=datetime.now(),
    )


def _window(qtbot, app: ApplicationService) -> MainWindow:
    window = MainWindow(app, auto_scan=False)
    window.runtime_timer.stop()
    window.log_timer.stop()
    qtbot.addWidget(window)
    window.show()
    return window


def test_remove_button_disabled_without_selection_and_enabled_when_safe(
    qtbot, tmp_path: Path
) -> None:
    app = _service(tmp_path)
    window = _window(qtbot, app)
    assert not window.actions["remove_script"].isEnabled()
    record = _individual(app, tmp_path / "脚本 安全.cmd")
    window.bind_records([record])
    assert window.actions["remove_script"].isEnabled()
    assert window.actions["remove_script"].text() == "移除脚本"
    assert (
        window.actions["remove_script"].property("semanticRole")
        == "destructive"
    )
    assert "回收站" in window.actions["remove_script"].toolTip()


@pytest.mark.parametrize(
    "state",
    [
        RuntimeState.STARTING,
        RuntimeState.PROCESS_RUNNING_PORT_CLOSED,
        RuntimeState.API_READY,
        RuntimeState.STOPPING,
        RuntimeState.DETACHED_UNVERIFIED,
    ],
)
def test_remove_button_disabled_for_unsafe_runtime_states(
    qtbot, tmp_path: Path, state: RuntimeState
) -> None:
    app = _service(tmp_path)
    record = _individual(app, tmp_path / f"{state.name}.bat")
    launch = _launch(record, state)
    app.launches[launch.launch_id] = launch
    window = _window(qtbot, app)
    window.bind_records([record])
    assert not window.actions["remove_script"].isEnabled()
    assert window.actions["remove_script"].property("semanticRole") == "neutral"
    assert "关联服务或启动任务" in window.actions["remove_script"].toolTip()
    # Avoid the production close-confirmation dialog during qtbot teardown.
    app.launches.clear()


def test_individual_removal_preserves_file_history_and_logs_but_clears_personal_data(
    tmp_path: Path
) -> None:
    app = _service(tmp_path)
    path = tmp_path / "中文 单独添加.bat"
    record = _individual(app, path)
    app.save_metadata(Metadata(
        canonical_path=record.fingerprint.canonical_path,
        favorite=True, tags=["RP"], notes="private note",
    ))
    app.trust(record)
    app.store.add_event(OperationEvent(kind="existing_history"))
    log = app.data_dir / "logs" / "old.log"
    log.parent.mkdir(parents=True)
    log.write_text("historical", encoding="utf-8")
    history_before = app.store.history_count()

    result = app.remove_script(record, ScriptRemovalMode.LIBRARY_ONLY)

    assert result.message == "脚本已从脚本库移除，原文件保持不变"
    assert path.exists()
    assert app.settings.individual_scripts == []
    assert record.fingerprint.canonical_path not in app.records
    assert app.metadata(record).tags == []
    assert not app.is_trusted(record)
    assert app.store.history_count() >= history_before
    assert log.read_text(encoding="utf-8") == "historical"


def test_directory_script_ignore_survives_scan_and_cancel_ignore_restores(
    tmp_path: Path
) -> None:
    app = _service(tmp_path)
    root = tmp_path / "扫描 根"
    path = _script(root / "角色 启动.cmd")
    app.add_root(root)
    record = app.rescan()[0]

    result = app.remove_script(record, ScriptRemovalMode.LIBRARY_ONLY)
    assert result.ignored
    assert path.exists()
    assert len(app.ignored_scripts()) == 1
    assert app.rescan() == []

    assert app.cancel_ignore(record.fingerprint.canonical_path)
    restored = app.rescan()
    assert len(restored) == 1
    assert restored[0].parsed.path == path.resolve()


def test_ignore_records_are_case_normalized_exact_and_malformed_paths_are_inert(
    tmp_path: Path
) -> None:
    app = _service(tmp_path)
    root = tmp_path / "Root"
    path = _script(root / "Exact.CMD")
    app.add_root(root)
    canonical = str(path.resolve())
    app.store.add_ignored_script(canonical.upper(), str(root.resolve()))
    app.store.add_ignored_script(canonical.lower(), str(root.resolve()))
    assert len(app.store.ignored_scripts()) == 1
    assert app.rescan() == []

    app.store.add_ignored_script(
        str((tmp_path / "outside.bat").resolve()), str(root.resolve())
    )
    app.store.add_ignored_script(
        str((root / "*.bat").resolve()), str(root.resolve())
    )
    valid = app.ignored_scripts()
    assert len(valid) == 1
    assert valid[0].canonical_path.casefold() == canonical.casefold()


def test_ignored_script_management_unignore_and_cleanup(
    qtbot, tmp_path: Path
) -> None:
    app = _service(tmp_path)
    root = tmp_path / "root"
    path = _script(root / "keep.bat")
    app.add_root(root)
    record = app.rescan()[0]
    app.remove_script(record, ScriptRemovalMode.LIBRARY_ONLY)
    dialog = IgnoredScriptsDialog(app)
    qtbot.addWidget(dialog)
    assert dialog.table.rowCount() == 1
    dialog.table.selectRow(0)
    dialog._unignore()
    assert dialog.changed
    assert app.ignored_scripts() == []

    missing = root / "missing.cmd"
    app.store.add_ignored_script(str(missing.resolve()), str(root.resolve()))
    dialog._refresh()
    assert dialog.table.rowCount() == 1
    dialog._cleanup()
    assert app.ignored_scripts() == []


def test_recycle_success_uses_adapter_and_never_permanent_delete(
    tmp_path: Path
) -> None:
    recycle = FakeRecycleBin()
    app = _service(tmp_path, recycle=recycle)
    path = tmp_path / "recycle me.bat"
    record = _individual(app, path)
    result = app.remove_script(record, ScriptRemovalMode.RECYCLE_BIN)
    assert result.recycled
    assert recycle.paths == [path.resolve()]
    # The fake adapter deliberately leaves it in place; the service never
    # calls unlink or any permanent-delete fallback.
    assert path.exists()


def test_recycle_failure_leaves_file_registration_metadata_and_trust(
    tmp_path: Path
) -> None:
    app = _service(
        tmp_path, recycle=FakeRecycleBin(OSError("trash unavailable"))
    )
    path = tmp_path / "failure.cmd"
    record = _individual(app, path)
    app.trust(record)
    app.save_metadata(Metadata(
        canonical_path=record.fingerprint.canonical_path,
        tags=["keep"],
    ))
    with pytest.raises(OSError, match="trash unavailable"):
        app.remove_script(record, ScriptRemovalMode.RECYCLE_BIN)
    assert path.exists()
    assert app.is_trusted(record)
    assert app.metadata(record).tags == ["keep"]
    assert app.settings.individual_scripts
    assert record.fingerprint.canonical_path in app.records


def test_active_process_race_is_rechecked_immediately_before_recycle(
    tmp_path: Path, monkeypatch
) -> None:
    recycle = FakeRecycleBin()
    app = _service(tmp_path, recycle=recycle)
    record = _individual(app, tmp_path / "race.bat")
    calls = iter([None, "该脚本仍有关联服务或启动任务，请先停止后再移除。"])
    monkeypatch.setattr(
        app, "script_removal_block_reason",
        lambda *_args, **_kwargs: next(calls),
    )
    with pytest.raises(LaunchInProgressError):
        app.remove_script(record, ScriptRemovalMode.RECYCLE_BIN)
    assert recycle.paths == []
    assert record.parsed.path.exists()


def test_service_blocks_active_or_pending_script_without_stopping_it(
    tmp_path: Path, monkeypatch
) -> None:
    app = _service(tmp_path)
    record = _individual(app, tmp_path / "active.bat")
    launch = _launch(record, RuntimeState.API_READY)
    app.launches[launch.launch_id] = launch
    monkeypatch.setattr(app, "reconcile_runtimes", lambda: {})
    with pytest.raises(LaunchInProgressError, match="请先停止"):
        app.remove_script(record, ScriptRemovalMode.LIBRARY_ONLY)
    assert record.parsed.path.exists()
    assert launch.state == RuntimeState.API_READY

    app.launches.clear()
    app._launching_scripts.add(record.fingerprint.canonical_path)
    with pytest.raises(LaunchInProgressError, match="启动任务"):
        app.remove_script(record, ScriptRemovalMode.LIBRARY_ONLY)
    assert record.parsed.path.exists()


def test_reparse_and_non_batch_candidates_are_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    recycle = FakeRecycleBin()
    app = _service(tmp_path, recycle=recycle)
    record = _individual(app, tmp_path / "link.bat")
    monkeypatch.setattr(app, "_is_reparse_point", lambda _path: True)
    with pytest.raises(ValueError, match="重解析点"):
        app.remove_script(record, ScriptRemovalMode.RECYCLE_BIN)

    monkeypatch.setattr(app, "_is_reparse_point", lambda _path: False)
    text = tmp_path / "not-script.txt"
    text.write_text("x", encoding="utf-8")
    record.parsed.path = text
    record.fingerprint = fingerprint(text)
    with pytest.raises(ValueError, match=r"\.bat"):
        app._validate_recycle_candidate(record)
    assert recycle.paths == []


def test_changed_script_identity_is_rejected_before_recycle(
    tmp_path: Path
) -> None:
    recycle = FakeRecycleBin()
    app = _service(tmp_path, recycle=recycle)
    record = _individual(app, tmp_path / "changed.bat")
    record.parsed.path.write_text("@echo off\nrem changed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="身份已变化"):
        app.remove_script(record, ScriptRemovalMode.RECYCLE_BIN)
    assert recycle.paths == []


def test_removal_dialog_defaults_to_non_destructive_choice(
    qtbot, tmp_path: Path
) -> None:
    app = _service(tmp_path)
    record = _individual(app, tmp_path / "dialog.bat")
    source = app.script_discovery_info(record)
    dialog = ScriptRemovalDialog(record, "Dialog", source)
    qtbot.addWidget(dialog)
    assert dialog.library_only.isDefault()
    assert dialog.choice is None
    assert dialog.library_only.text() == "仅从脚本库移除"
    assert "Windows 回收站" in dialog.recycle.text()


def test_gui_removal_selects_nearest_and_refreshes_model_references(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    app = _service(tmp_path)
    first = _individual(app, tmp_path / "first.bat")
    second = _individual(app, tmp_path / "second.bat")
    window = _window(qtbot, app)
    window.bind_records([first, second])
    window.select_record(first)
    synced: list[list] = []

    class ModelPage:
        def sync_references(self, records):
            synced.append(records)

        def shutdown(self):
            pass

    window._model_library_page = ModelPage()

    class AcceptedDialog:
        choice = ScriptRemovalMode.LIBRARY_ONLY

        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

    def immediate(function, on_success, on_error=None, on_finished=None):
        try:
            on_success(function())
        except Exception as error:
            if on_error:
                on_error(error)
        if on_finished:
            on_finished()
        return object()

    monkeypatch.setattr(
        "llmbatdesk.qt.main_window.ScriptRemovalDialog", AcceptedDialog
    )
    monkeypatch.setattr(window, "_run_async", immediate)
    window.remove_script_requested()
    assert window.selected_record == second
    assert first.parsed.path.exists()
    assert synced and synced[-1] == [second]
    assert window._model_library_page is not None


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_remove_action_theme_and_no_new_timer(
    qapp, qtbot, tmp_path: Path, theme: str
) -> None:
    apply_theme(qapp, theme)
    app = _service(tmp_path)
    record = _individual(app, tmp_path / f"{theme}.bat")
    window = _window(qtbot, app)
    before = len(window.findChildren(QTimer))
    window.bind_records([record])
    button = window.actions["remove_script"]
    assert button.property("semanticRole") == "destructive"
    assert button.isEnabled()
    assert len(window.findChildren(QTimer)) == before
