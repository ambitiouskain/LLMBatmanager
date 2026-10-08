from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from llmbatdesk.discovery import ScriptScanner
from llmbatdesk.domain.models import (
    ApiCheckResult, ApiReadinessStatus, Backend, LaunchConfirmationMode, LaunchMode,
    LaunchModeOverride, ManagedLaunch, PortOccupant, ProcessIdentity, RuntimeState,
)
from llmbatdesk.runtime.api import HttpxNetworkChecker
from llmbatdesk.runtime.cleanup import cleanup_logs
from llmbatdesk.runtime.processes import SubprocessExecutor
from llmbatdesk.services import ApplicationService
from llmbatdesk.settings import SettingsStore
from llmbatdesk.storage import MetadataStore


def service(tmp_path: Path, **kwargs) -> ApplicationService:
    data = tmp_path / "data"
    return ApplicationService(
        data_dir=data,
        settings_store=SettingsStore(data),
        metadata_store=MetadataStore(data / "llmbatdesk.db"),
        **kwargs,
    )


def runnable_record(tmp_path: Path, *, extra: str = ""):
    tmp_path.mkdir(parents=True, exist_ok=True)
    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    script = tmp_path / "run.bat"
    executable.write_bytes(b"fake")
    model.write_bytes(b"fake")
    script.write_text(
        f'"{executable}" --model "{model}" --port 8080 {extra}\n', encoding="utf-8"
    )
    return ScriptScanner().scan([], [script])[0]


def test_cleanup_oldest_first_and_never_active_log(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old, middle, active = (log_dir / name for name in ("old.log", "middle.log", "active.log"))
    old.write_bytes(b"x" * 10)
    middle.write_bytes(b"x" * 20)
    active.write_bytes(b"x" * 30)
    now = datetime.now().timestamp()
    os.utime(old, (now - 1000, now - 1000))
    os.utime(middle, (now - 500, now - 500))
    os.utime(active, (now - 2000, now - 2000))
    result = cleanup_logs(
        log_dir, retention_days=None, max_total_bytes=30, max_count=1,
        protected_paths={active},
    )
    assert result.deleted_count == 2
    assert active.exists()
    assert not old.exists() and not middle.exists()


def test_retention_age_and_unlimited_options(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    expired = log_dir / "expired.log"
    expired.write_text("old", encoding="utf-8")
    timestamp = (datetime.now() - timedelta(days=40)).timestamp()
    os.utime(expired, (timestamp, timestamp))
    assert cleanup_logs(
        log_dir, retention_days=None, max_total_bytes=None, max_count=None
    ).deleted_count == 0
    assert cleanup_logs(
        log_dir, retention_days=30, max_total_bytes=None, max_count=None
    ).deleted_count == 1


def test_storage_stats_history_temporary_and_safe_clear(tmp_path: Path) -> None:
    app = service(tmp_path)
    (app.data_dir / "logs").mkdir()
    (app.data_dir / "logs" / "x.log").write_text("日志", encoding="utf-8")
    (app.data_dir / "temporary").mkdir()
    (app.data_dir / "temporary" / "x.bat").write_text("echo x", encoding="utf-8")
    from llmbatdesk.domain.models import OperationEvent
    app.store.add_event(OperationEvent(kind="test"))
    stats = app.storage_stats()
    assert stats.log_count == 1 and stats.log_size > 0
    assert stats.history_count == 1 and stats.temporary_size > 0
    assert app.clear_launch_history() == 1
    assert app.clear_temporary_files().deleted_count == 1


def test_delete_all_data_requires_typed_confirmation(tmp_path: Path) -> None:
    app = service(tmp_path)
    marker = app.data_dir / "marker.txt"
    marker.write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError):
        app.delete_all_user_data("yes")
    assert marker.exists()
    app.delete_all_user_data("删除全部数据")
    assert not marker.exists()
    assert app.store.path.exists()  # empty live database recreated, executable untouched


def test_settings_defaults_and_metadata_launch_override_persist(tmp_path: Path) -> None:
    app = service(tmp_path)
    assert app.settings.log_retention_days == 30
    assert app.settings.log_max_total_mb == 200
    assert app.settings.log_max_count == 100
    assert app.settings.launch_mode == LaunchMode.BACKGROUND
    record = runnable_record(tmp_path)
    metadata = app.metadata(record)
    metadata.launch_mode_override = LaunchModeOverride.VISIBLE
    app.save_metadata(metadata)
    assert app.metadata(record).launch_mode_override == LaunchModeOverride.VISIBLE
    assert app.effective_launch_mode(record) == LaunchMode.VISIBLE


def test_background_and_visible_creation_flags_without_real_process(
    monkeypatch, tmp_path: Path
) -> None:
    script = tmp_path / "fake.bat"
    script.write_text("@echo off", encoding="utf-8")
    calls: list[dict] = []

    class FakePopen:
        pid = 123

        def __init__(self, _command, **kwargs):
            calls.append(kwargs)

    class FakePsutilProcess:
        def __init__(self, _pid):
            pass

        def create_time(self):
            return 10.0

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr("llmbatdesk.runtime.processes.psutil.Process", FakePsutilProcess)
    executor = SubprocessExecutor()
    executor.start(script, tmp_path, None, None, LaunchMode.BACKGROUND)
    assert calls[-1]["shell"] is False
    assert calls[-1]["creationflags"] & subprocess.CREATE_NO_WINDOW
    executor.start(script, tmp_path, None, None, LaunchMode.VISIBLE)
    assert calls[-1]["creationflags"] & subprocess.CREATE_NEW_CONSOLE
    assert calls[-1]["stdout"] is None and calls[-1]["stdin"] is None


def test_interactive_detection_and_confirmation_modes(tmp_path: Path) -> None:
    app = service(tmp_path)
    record = runnable_record(tmp_path, extra="\npause\nchoice /c YN")
    assert record.parsed.interactive_commands == ["pause", "choice"]
    app.trust(record)
    assert app.should_confirm_launch(record)  # hidden input warning is never bypassed
    plain = runnable_record(tmp_path / "plain")
    assert app.should_confirm_launch(plain)
    app.trust(plain)
    assert not app.should_confirm_launch(plain)
    app.settings.launch_confirmation_mode = LaunchConfirmationMode.ALWAYS
    assert app.should_confirm_launch(plain)
    app.settings.launch_confirmation_mode = LaunchConfirmationMode.TRUSTED_SKIP
    assert app.should_confirm_launch(plain, override=object())
    prompt = runnable_record(tmp_path / "prompt", extra="\nset /p ANSWER=继续吗")
    assert "set /p" in prompt.parsed.interactive_commands


def test_trust_invalidates_for_content_and_working_directory(tmp_path: Path) -> None:
    app = service(tmp_path)
    record = runnable_record(tmp_path)
    app.trust(record)
    assert app.is_trusted(record)
    assert not app.is_trusted(record, tmp_path / "different")
    record.parsed.path.write_text(record.parsed.raw_text + "\nrem changed", encoding="utf-8")
    changed = ScriptScanner().scan([], [record.parsed.path])[0]
    assert not app.is_trusted(changed)


def test_openai_models_health_auth_404_and_timeout_diagnostics() -> None:
    seen: list[str] = []

    def health_transport(request):
        seen.append(request.url.path)
        return httpx.Response(
            404 if request.url.path == "/v1/models" else 200,
            json={}, request=request,
        )

    result = HttpxNetworkChecker(
        httpx.Client(transport=httpx.MockTransport(health_transport))
    ).check("http://127.0.0.1:8080/v1", Backend.LLAMA_CPP)
    assert result.ready and seen == ["/v1/models", "/health"]
    assert result.checked_url.endswith("/health")

    auth = HttpxNetworkChecker(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, request=request)
    ))).check("http://localhost:8080/v1", Backend.LLAMA_CPP)
    assert auth.state == RuntimeState.AUTH_REQUIRED
    assert auth.status_code == 401 and auth.error_kind == "authentication_required"

    missing = HttpxNetworkChecker(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(404, request=request)
    ))).check("http://localhost:8080/v1", Backend.LLAMA_CPP)
    assert missing.status_code == 404 and missing.error_kind == "incompatible_endpoint"

    timeout = HttpxNetworkChecker(httpx.Client(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=request))
    ))).check("http://localhost:8080/v1", Backend.LLAMA_CPP)
    assert timeout.error_kind == "timeout"

    loading = HttpxNetworkChecker(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(503, request=request)
    ))).check("http://localhost:8080/v1", Backend.LLAMA_CPP)
    assert not loading.ready
    assert loading.state == RuntimeState.PORT_LISTENING_API_NOT_READY
    assert loading.status_code == 503


def test_stale_api_result_cannot_restore_old_state(tmp_path: Path) -> None:
    app = service(tmp_path)
    launch = ManagedLaunch(
        launch_id="api", script_path="x.bat", script_hash="h",
        identity=ProcessIdentity(pid=1, create_time=1), log_path="x.log",
        state=RuntimeState.PORT_LISTENING_API_NOT_READY, verified=True,
    )
    launch.api_check_generation = 2
    ready = ApiCheckResult(
        ready=True, state=RuntimeState.API_READY, status_code=200,
        checked_url="http://127.0.0.1:8081/v1/models", retry_count=2,
    )
    assert app.apply_api_result(launch, ready, 2)
    stale = ApiCheckResult(
        ready=False, state=RuntimeState.PORT_LISTENING_API_NOT_READY,
        status_code=404, error_kind="incompatible_endpoint",
    )
    assert not app.apply_api_result(launch, stale, 1)
    assert launch.state == RuntimeState.API_READY
    assert launch.api_status == ApiReadinessStatus.READY
    assert launch.api_last_http_status == 200


def test_api_check_uses_actual_runtime_port(tmp_path: Path) -> None:
    launcher = ProcessIdentity(pid=10, create_time=10, executable="cmd.exe")
    server = ProcessIdentity(
        pid=20, create_time=20, executable=str(tmp_path / "llama-server.exe")
    )

    class Processes:
        def identity(self, pid):
            return {10: launcher, 20: server}.get(pid)

        def children(self, _pid):
            return [server]

    class Ports:
        def inspect(self, port):
            return PortOccupant(port=port, pid=20)

    class Network:
        address = ""

        def check(self, address, _backend):
            self.address = address
            return ApiCheckResult(
                ready=True, state=RuntimeState.API_READY, status_code=200,
                checked_url=address.rstrip("/v1") + "/v1/models",
            )

    network = Network()
    app = service(
        tmp_path, process_inspector=Processes(), port_inspector=Ports(),
        network_checker=network,
    )
    record = runnable_record(tmp_path)
    launch = ManagedLaunch(
        launch_id="actual", script_path=str(record.parsed.path),
        script_hash=record.fingerprint.sha256, identity=launcher,
        server_identity=server, child_identities=[server], configured_port=8080,
        actual_port=8081, log_path=str(tmp_path / "x.log"),
        state=RuntimeState.PORT_LISTENING_API_NOT_READY, verified=True,
    )
    app.launches[launch.launch_id] = launch
    app.check_api(launch, record.parsed)
    assert network.address == "http://127.0.0.1:8081/v1"
