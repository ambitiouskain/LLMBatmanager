from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from llmbatdesk.domain.models import RuntimeState
from llmbatdesk.qt.main_window import MainWindow
from llmbatdesk.services import ApplicationService
from llmbatdesk.settings import AppSettings, SettingsStore


def _window(qtbot, tmp_path: Path, settings: AppSettings) -> MainWindow:
    store = SettingsStore(tmp_path)
    store.save(settings)
    service = ApplicationService(data_dir=tmp_path, settings_store=store)
    window = MainWindow(service, auto_scan=False)
    qtbot.addWidget(window)
    return window


def test_disabled_extension_has_no_tab_database_or_lazy_import(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []
    real_import = importlib.import_module

    def guarded(name: str, *args, **kwargs):
        if name.startswith("llmbatdesk.extensions.model_library"):
            calls.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", guarded)
    window = _window(qtbot, tmp_path, AppSettings())
    assert [window.main_tabs.tabText(i) for i in range(window.main_tabs.count())] == ["脚本库"]
    assert not (tmp_path / "model_library.sqlite").exists()
    assert calls == []


def test_enabled_but_unopened_extension_has_tab_without_database_or_worker(
    qtbot, tmp_path: Path
) -> None:
    window = _window(
        qtbot, tmp_path, AppSettings(model_library_enabled=True)
    )
    assert window.main_tabs.tabText(1) == "模型库"
    assert window._model_library_page is None
    assert not (tmp_path / "model_library.sqlite").exists()


def test_opening_enabled_tab_initializes_once_and_disabling_preserves_database(
    qtbot, tmp_path: Path
) -> None:
    window = _window(
        qtbot, tmp_path, AppSettings(model_library_enabled=True)
    )
    window.main_tabs.setCurrentIndex(1)
    qtbot.waitUntil(lambda: window._model_library_page is not None)
    database = tmp_path / "model_library.sqlite"
    assert database.exists()
    page = window._model_library_page
    window.main_tabs.setCurrentIndex(0)
    window.main_tabs.setCurrentIndex(1)
    assert window._model_library_page is page
    window.service.settings.model_library_enabled = False
    window._sync_model_library_tab()
    assert window.main_tabs.count() == 1
    assert database.exists()


def test_extension_initialization_failure_does_not_break_script_library(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    window = _window(
        qtbot, tmp_path, AppSettings(model_library_enabled=True)
    )
    monkeypatch.setattr(
        importlib, "import_module",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("broken")),
    )
    window.main_tabs.setCurrentIndex(1)
    assert window._model_library_page is None
    assert "broken" in window._model_library_error
    assert window.main_tabs.tabText(0) == "脚本库"


@pytest.mark.parametrize(
    "state",
    [
        RuntimeState.STARTING,
        RuntimeState.PROCESS_RUNNING_PORT_CLOSED,
        RuntimeState.PORT_LISTENING_API_NOT_READY,
        RuntimeState.API_READY,
        RuntimeState.STOPPING,
        RuntimeState.DETACHED_UNVERIFIED,
    ],
)
def test_core_runtime_states_suppress_extension_scans(
    qtbot, tmp_path: Path, state: RuntimeState
) -> None:
    window = _window(
        qtbot, tmp_path, AppSettings(model_library_enabled=True)
    )
    window.service.launches = {"fake": SimpleNamespace(state=state)}
    assert window._model_operation_active()
    window.main_tabs.setCurrentIndex(1)
    page = window._model_library_page
    assert page is not None
    with pytest.raises(Exception, match="模型运行期间"):
        page.extension_service.begin_scan()
    window.service.launches = {}
    window.close()
