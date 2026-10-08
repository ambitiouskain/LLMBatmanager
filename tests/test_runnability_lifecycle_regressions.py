from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from llmbatdesk.discovery import ScriptScanner
from llmbatdesk.domain.models import (
    ManagedLaunch, PortOccupant, ProcessIdentity, RunnabilityStatus, RuntimeState,
)
from llmbatdesk.parsing import parse_script_bytes
from llmbatdesk.runnability import validate_runnability
from llmbatdesk.runtime.lifecycle import RuntimeReconciler
from llmbatdesk.runtime.ports import ConflictDecision, PortConflictError
from llmbatdesk.services import (
    ApplicationService, LaunchBlockedError, LaunchInProgressError,
)
from llmbatdesk.settings import AppSettings, SettingsStore
from llmbatdesk.storage import MetadataStore


class FakeProcesses:
    def __init__(self) -> None:
        self.table: dict[int, ProcessIdentity] = {}
        self.child_table: dict[int, list[ProcessIdentity]] = {}
        self.terminated: list[tuple[int, bool]] = []

    def identity(self, pid: int):
        return self.table.get(pid)

    def children(self, pid: int):
        return list(self.child_table.get(pid, []))

    def terminate(self, identity: ProcessIdentity, force: bool = False):
        if self.table.get(identity.pid) != identity:
            return False
        self.terminated.append((identity.pid, force))
        self.table.pop(identity.pid, None)
        return True

    def wait(self, identity: ProcessIdentity, _timeout: float):
        return identity.pid not in self.table


class FakePorts:
    def __init__(self, occupants: dict[int, PortOccupant] | None = None) -> None:
        self.occupants = occupants or {}
        self.inspected: list[int] = []

    def inspect(self, port: int):
        self.inspected.append(port)
        return self.occupants.get(port)

    def find_free(self, start: int, host: str = "127.0.0.1"):
        return next(port for port in range(start, 65536) if port not in self.occupants)


def identity(pid: int, executable: str, create_time: float | None = None) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid, create_time=create_time if create_time is not None else float(pid),
        executable=executable, command_line=[executable],
    )


def managed(
    parent: ProcessIdentity,
    server: ProcessIdentity | None = None,
    state: RuntimeState = RuntimeState.STARTING,
) -> ManagedLaunch:
    return ManagedLaunch(
        launch_id="launch", script_path="x.bat", script_hash="hash",
        identity=parent, child_identities=[server] if server else [],
        server_identity=server, configured_port=8080, actual_port=8080,
        log_path="x.log", state=state, started_at=datetime.now() - timedelta(seconds=30),
    )


def test_explicit_llama_executable_and_model_missing_but_parseable(tmp_path: Path) -> None:
    parsed = parse_script_bytes(
        b'"Z:\\missing\\llama-server.exe" --model "Z:\\missing\\model.gguf" --port 8080',
        tmp_path / "copied.bat",
    )
    report = validate_runnability(parsed, which=lambda _name: None)
    assert parsed.configured_port == 8080
    assert report.status == RunnabilityStatus.EXECUTABLE_AND_MODEL_MISSING
    assert not report.launch_allowed
    assert any("可执行文件不存在" in warning for warning in report.warnings)
    assert any("模型文件不存在" in warning for warning in report.warnings)


def test_bare_llama_name_uses_path_and_missing_blocks(tmp_path: Path) -> None:
    parsed = parse_script_bytes(
        b"llama-server.exe --model missing.gguf --port 8080", tmp_path / "copied.bat"
    )
    report = validate_runnability(parsed, which=lambda _name: None)
    assert report.executable_exists is False
    assert not report.launch_allowed


def test_ollama_unavailable_stays_parseable_but_not_runnable(tmp_path: Path) -> None:
    parsed = parse_script_bytes(b"ollama serve", tmp_path / "ollama.cmd")
    report = validate_runnability(parsed, which=lambda _name: None)
    assert parsed.model_name is None
    assert report.status == RunnabilityStatus.EXECUTABLE_MISSING
    assert not report.launch_allowed


def test_existing_executable_and_regular_model_are_runnable(tmp_path: Path) -> None:
    executable, model = tmp_path / "llama-server.exe", tmp_path / "model.gguf"
    executable.write_bytes(b"fake")
    model.write_bytes(b"fake")
    parsed = parse_script_bytes(
        f'"{executable}" --model "{model}" --port 8080'.encode(), tmp_path / "x.bat"
    )
    report = validate_runnability(parsed)
    assert report.status == RunnabilityStatus.RUNNABLE
    assert report.launch_allowed


def test_model_directory_is_not_accepted_as_model_file(tmp_path: Path) -> None:
    executable, model_dir = tmp_path / "llama-server.exe", tmp_path / "model.gguf"
    executable.write_bytes(b"fake")
    model_dir.mkdir()
    parsed = parse_script_bytes(
        f'"{executable}" --model "{model_dir}" --port 8080'.encode(), tmp_path / "x.bat"
    )
    assert validate_runnability(parsed).status == RunnabilityStatus.MODEL_MISSING


@pytest.mark.parametrize(
    "actual",
    [
        None,
        identity(10, "unrelated.exe", create_time=200.0),
        identity(10, "cmd.exe", create_time=101.0),
    ],
)
def test_stored_missing_or_reused_pid_becomes_stale(actual: ProcessIdentity | None) -> None:
    processes, ports = FakeProcesses(), FakePorts()
    if actual:
        processes.table[10] = actual
    launch = managed(identity(10, "cmd.exe", create_time=100.0))
    RuntimeReconciler(processes, ports).reconcile(launch, startup=True)
    assert launch.state == RuntimeState.STALE_RECORD
    assert not launch.verified


def test_cmd_launcher_alone_is_not_an_active_server() -> None:
    processes, ports = FakeProcesses(), FakePorts()
    parent = identity(10, "cmd.exe")
    processes.table[parent.pid] = parent
    launch = managed(parent)
    RuntimeReconciler(processes, ports).reconcile(launch, startup=True)
    assert launch.state == RuntimeState.STALE_RECORD
    assert not launch.verified


def test_launcher_exit_before_server_is_failed() -> None:
    launch = managed(identity(10, "cmd.exe"))
    RuntimeReconciler(FakeProcesses(), FakePorts()).reconcile(launch)
    assert launch.state == RuntimeState.FAILED
    assert "启动进程已退出" in launch.failure_reason


def test_server_survives_launcher_and_reattaches_by_identity_and_port() -> None:
    processes = FakeProcesses()
    server = identity(20, "llama-server.exe")
    processes.table[20] = server
    ports = FakePorts({8080: PortOccupant(port=8080, pid=20)})
    launch = managed(identity(10, "cmd.exe"), server)
    RuntimeReconciler(processes, ports).reconcile(launch, startup=True)
    assert launch.verified
    assert launch.state == RuntimeState.PORT_LISTENING_API_NOT_READY


def test_wrong_pid_owning_expected_port_is_not_managed() -> None:
    processes = FakeProcesses()
    server = identity(20, "llama-server.exe")
    processes.table[20] = server
    launch = managed(identity(10, "cmd.exe"), server)
    RuntimeReconciler(
        processes, FakePorts({8080: PortOccupant(port=8080, pid=99)})
    ).reconcile(launch, startup=True)
    assert launch.state == RuntimeState.STALE_RECORD
    assert not launch.verified


def service_with_fakes(
    tmp_path: Path, processes: FakeProcesses, ports: FakePorts, executor: object | None = None,
) -> ApplicationService:
    data = tmp_path / "data"
    return ApplicationService(
        data_dir=data, settings_store=SettingsStore(data),
        metadata_store=MetadataStore(data / "db.sqlite"),
        process_inspector=processes, port_inspector=ports, executor=executor,
    )


def test_stale_persisted_record_never_appears_active(tmp_path: Path) -> None:
    data = tmp_path / "data"
    store = MetadataStore(data / "db.sqlite")
    store.save_launch(managed(identity(10, "cmd.exe")))
    service = ApplicationService(
        data_dir=data, settings_store=SettingsStore(data), metadata_store=store,
        process_inspector=FakeProcesses(), port_inspector=FakePorts(),
    )
    assert service.active_launches() == {}
    assert service.launches["launch"].state == RuntimeState.STALE_RECORD


def test_missing_environment_blocks_before_executor_and_creates_no_launch(tmp_path: Path) -> None:
    class NeverExecutor:
        calls = 0
        def start(self, *_args):
            self.calls += 1
            raise AssertionError("must not execute")
    script = tmp_path / "copied.bat"
    script.write_text(
        '"Z:\\missing\\llama-server.exe" --model "Z:\\missing\\model.gguf" --port 8080',
        encoding="utf-8",
    )
    executor = NeverExecutor()
    service = service_with_fakes(tmp_path, FakeProcesses(), FakePorts(), executor)
    record = ScriptScanner().scan([], [script])[0]
    service.trust(record)
    with pytest.raises(LaunchBlockedError):
        service.launch(record)
    assert executor.calls == 0
    assert service.active_launches() == {}
    assert service.launches == {}
    blocked = service.history_entries()
    assert len(blocked) == 1
    assert blocked[0].state == "启动已阻止"
    assert "可执行文件不存在" in blocked[0].reason


def test_double_start_is_deduplicated_and_cmd_only_is_not_active(tmp_path: Path) -> None:
    processes, ports = FakeProcesses(), FakePorts()
    parent = identity(10, "cmd.exe")
    class Executor:
        calls = 0
        def start(self, *_args):
            self.calls += 1
            processes.table[10] = parent
            return parent
    executable, model, script = (
        tmp_path / "llama-server.exe", tmp_path / "model.gguf", tmp_path / "x.bat"
    )
    executable.write_bytes(b"fake")
    model.write_bytes(b"fake")
    script.write_text(f'"{executable}" --model "{model}" --port 8080', encoding="utf-8")
    executor = Executor()
    service = service_with_fakes(tmp_path, processes, ports, executor)
    record = ScriptScanner().scan([], [script])[0]
    service.trust(record)
    first = service.launch(record)
    with pytest.raises(LaunchInProgressError):
        service.launch(record)
    assert executor.calls == 1
    assert first.state == RuntimeState.STARTING
    assert service.active_launches() == {}
    assert len(service.history_launches()) == 1


def test_atomic_port_conflict_happens_before_executor_for_partial_script(tmp_path: Path) -> None:
    processes = FakeProcesses()
    class NeverExecutor:
        calls = 0
        def start(self, *_args):
            self.calls += 1
            raise AssertionError
    executable, model, script = (
        tmp_path / "llama-server.exe", tmp_path / "model.gguf", tmp_path / "partial.bat"
    )
    executable.write_bytes(b"fake")
    model.write_bytes(b"fake")
    script.write_text(
        f'call setup.cmd\n"{executable}" --model "{model}" --port 8080', encoding="utf-8"
    )
    executor = NeverExecutor()
    ports = FakePorts({8080: PortOccupant(port=8080, pid=99)})
    service = service_with_fakes(tmp_path, processes, ports, executor)
    record = ScriptScanner().scan([], [script])[0]
    assert record.parsed.configured_port == 8080
    assert record.parsed.confidence.value == "部分解析"
    service.trust(record)
    with pytest.raises(PortConflictError):
        service.launch(record)
    assert executor.calls == 0
    assert ports.inspected[-1] == 8080


def test_repeated_reconciliation_and_callbacks_cannot_duplicate_active_id() -> None:
    processes = FakeProcesses()
    server = identity(20, "llama-server.exe")
    processes.table[20] = server
    ports = FakePorts({8080: PortOccupant(port=8080, pid=20)})
    launch = managed(identity(10, "cmd.exe"), server)
    reconciler = RuntimeReconciler(processes, ports)
    for _ in range(5):
        reconciler.reconcile(launch, startup=True)
    rows = {launch.launch_id: launch}
    assert list(rows) == ["launch"]
    assert launch.verified


def test_all_managed_conflict_decisions_are_implemented(tmp_path: Path) -> None:
    service = service_with_fakes(tmp_path, FakeProcesses(), FakePorts())
    occupant = PortOccupant(port=8080, pid=20, managed_launch_id="current")
    stopped: list[str] = []
    service.stop = lambda launch_id: stopped.append(launch_id) or True  # type: ignore[method-assign]
    assert service.handle_conflict_decision(ConflictDecision.STOP_AND_START, occupant) == "start"
    assert service.handle_conflict_decision(ConflictDecision.STOP_ONLY, occupant) == "stopped"
    assert service.handle_conflict_decision(ConflictDecision.ALTERNATE_PORT, occupant) == "alternate"
    assert service.handle_conflict_decision(ConflictDecision.CANCEL, occupant) == "cancel"
    assert stopped == ["current", "current"]


def test_unmanaged_conflict_decision_cannot_stop_process(tmp_path: Path) -> None:
    service = service_with_fakes(tmp_path, FakeProcesses(), FakePorts())
    with pytest.raises(PermissionError):
        service.handle_conflict_decision(
            ConflictDecision.STOP_AND_START, PortOccupant(port=8080, pid=99)
        )


def test_two_intentional_launches_on_different_ports_are_two_active_rows(tmp_path: Path) -> None:
    processes, ports = FakeProcesses(), FakePorts()
    executable, model = tmp_path / "llama-server.exe", tmp_path / "model.gguf"
    executable.write_bytes(b"fake")
    model.write_bytes(b"fake")
    scripts = []
    for port in (8080, 8081):
        script = tmp_path / f"x-{port}.bat"
        script.write_text(
            f'"{executable}" --model "{model}" --port {port}', encoding="utf-8"
        )
        scripts.append(script)
    class Executor:
        calls = 0
        def start(self, _script, *_args):
            self.calls += 1
            parent = identity(100 + self.calls, "cmd.exe")
            server = identity(200 + self.calls, str(executable))
            processes.table[parent.pid] = parent
            processes.table[server.pid] = server
            processes.child_table[parent.pid] = [server]
            port = 8079 + self.calls
            ports.occupants[port] = PortOccupant(port=port, pid=server.pid)
            return parent
    service = service_with_fakes(tmp_path, processes, ports, Executor())
    records = ScriptScanner().scan([], scripts)
    for record in records:
        service.trust(record)
        service.launch(record)
    assert len(service.active_launches()) == 2
    assert len(set(service.active_launches())) == 2
    assert {item.actual_port for item in service.active_launches().values()} == {8080, 8081}


def test_stopped_launch_leaves_active_table_and_remains_history(tmp_path: Path) -> None:
    processes = FakeProcesses()
    parent, server = identity(10, "cmd.exe"), identity(20, "llama-server.exe")
    processes.table = {10: parent, 20: server}
    processes.child_table[10] = [server]
    ports = FakePorts({8080: PortOccupant(port=8080, pid=20)})
    service = service_with_fakes(tmp_path, processes, ports)
    launch = managed(parent, server, RuntimeState.PORT_LISTENING_API_NOT_READY)
    launch.verified = True
    service.launches[launch.launch_id] = launch
    assert service.stop(launch.launch_id)
    assert service.active_launches() == {}
    assert service.history_launches()[launch.launch_id].state == RuntimeState.STOPPED
