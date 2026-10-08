from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from llmbatdesk.discovery import ScriptScanner
from llmbatdesk.domain.models import (
    ApiCheckResult, Backend, ManagedLaunch, ParseConfidence, ProcessIdentity,
    RuntimeState,
)
from llmbatdesk.parsing import parse_script_bytes
from llmbatdesk.runtime.api import HttpxNetworkChecker, poll_readiness
from llmbatdesk.runtime.cleanup import cleanup_temporary_files
from llmbatdesk.runtime.lifecycle import _same_executable
from llmbatdesk.runtime.logging import redact
from llmbatdesk.runtime.override import generate_port_override
from llmbatdesk.runtime.processes import ProcessTracker
from llmbatdesk.services import ApplicationService, LaunchInProgressError
from llmbatdesk.settings import SettingsStore
from llmbatdesk.storage import MetadataStore


def parse(text: str, *, encoding: str = "utf-8", newline: str = "\n"):
    return parse_script_bytes(
        text.replace("\n", newline).encode(encoding),
        Path("C:/脚本/run.bat"),
    )


def test_quoted_special_character_paths_are_not_dynamic() -> None:
    result = parse(
        '"C:\\LLM (正式)&工具!\\llama-server.exe" '
        '--model "C:\\模型 (Q4)&正式!\\model.gguf" --port 9876'
    )
    assert result.confidence == ParseConfidence.FULL
    assert result.executable == r"C:\LLM (正式)&工具!\llama-server.exe"
    assert result.model_path == r"C:\模型 (Q4)&正式!\model.gguf"


@pytest.mark.parametrize(
    "script",
    [
        "llama-server.exe -m x.gguf --port 8080 & echo second",
        "llama-server.exe -m x.gguf --port 8080 | more",
        "if exist x.gguf (llama-server.exe -m x.gguf --port 8080)",
        'cmd /c "llama-server.exe -m x.gguf --port 8080"',
        'powershell -Command "llama-server.exe -m x.gguf --port 8080"',
    ],
)
def test_control_flow_and_wrappers_never_claim_full_or_safe_override(script: str) -> None:
    result = parse(script)
    assert result.confidence != ParseConfidence.FULL
    assert result.port_source is None


def test_multiple_server_commands_and_repeated_ports_are_ambiguous() -> None:
    multiple = parse(
        "llama-server.exe -m a.gguf --port 8080\n"
        "llama-server.exe -m b.gguf --port 8081"
    )
    repeated = parse("llama-server.exe -m x.gguf --port 8080 --port=8081")
    assert multiple.confidence == ParseConfidence.PARTIAL
    assert multiple.port_source is None
    assert repeated.confidence == ParseConfidence.PARTIAL
    assert repeated.port_source is None


def test_short_p_port_and_odd_caret_continuation() -> None:
    result = parse(
        "llama-server.exe ^^^\n"
        " -m x.gguf ^\n"
        " -p 9876",
        newline="\r\n",
    )
    assert result.configured_port == 9876
    assert result.port_source is not None
    assert result.port_source.style == "argument"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gbk", "cp932"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_common_encodings_and_newlines_parse_without_execution(
    encoding: str, newline: str
) -> None:
    result = parse(
        '"C:\\模型\\llama-server.exe" -m "C:\\模型\\model.gguf" --port=9876',
        encoding=encoding,
        newline=newline,
    )
    assert result.backend == Backend.LLAMA_CPP
    assert result.configured_port == 9876


def test_invalid_models_body_does_not_claim_models_endpoint_ready() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, content=b"not-json", request=request)
        return httpx.Response(404, request=request)

    result = HttpxNetworkChecker(
        httpx.Client(transport=httpx.MockTransport(transport))
    ).check("http://127.0.0.1:8080/v1", Backend.LLAMA_CPP)
    assert not result.ready
    assert result.error_kind == "incompatible_endpoint"


def test_quoted_api_keys_with_spaces_are_fully_redacted() -> None:
    text = '--api-key "secret value" API_KEY="another secret"\n'
    redacted = redact(text)
    assert "secret value" not in redacted
    assert "another secret" not in redacted
    assert redacted.count("<已隐藏>") == 2


def test_large_model_response_is_bounded() -> None:
    body = {"data": [{"id": f"model-{index}"} for index in range(5000)]}
    checker = HttpxNetworkChecker(httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body, request=request)
    )))
    result = checker.check("http://127.0.0.1:8080/v1", Backend.LLAMA_CPP)
    assert len(result.loaded_models) <= 100


def test_process_exit_during_request_cannot_report_ready() -> None:
    alive = True

    class Checker:
        def check(self, *_args):
            nonlocal alive
            alive = False
            return ApiCheckResult(ready=True, state=RuntimeState.API_READY)

    result = poll_readiness(
        Checker(), "http://127.0.0.1:8080/v1", Backend.LLAMA_CPP,
        timeout=1, interval=0, process_alive=lambda: alive,
    )
    assert not result.ready
    assert result.error_kind == "process_exited"


def test_terminal_launch_rejects_late_api_callback(tmp_path: Path) -> None:
    service = ApplicationService(
        data_dir=tmp_path,
        settings_store=SettingsStore(tmp_path),
        metadata_store=MetadataStore(tmp_path / "db.sqlite"),
    )
    launch = ManagedLaunch(
        launch_id="stopped", script_path="x.bat", script_hash="h",
        identity=ProcessIdentity(pid=1, create_time=1), log_path="x.log",
        state=RuntimeState.STOPPED, api_check_generation=4,
    )
    ready = ApiCheckResult(ready=True, state=RuntimeState.API_READY)
    assert not service.apply_api_result(launch, ready, 4)
    assert launch.state == RuntimeState.STOPPED


def test_temporary_cleanup_never_follows_file_symlink_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "temporary"
    root.mkdir()
    outside = tmp_path / "outside.bat"
    outside.write_text("do not delete", encoding="utf-8")
    link = root / "linked.bat"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 配置不允许创建符号链接")
    cleanup_temporary_files(root)
    assert outside.read_text(encoding="utf-8") == "do not delete"
    assert link.exists()


def test_persisted_temporary_path_cannot_delete_outside_data_dir(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    outside = tmp_path / "outside.bat"
    outside.write_text("keep", encoding="utf-8")
    service = ApplicationService(
        data_dir=data,
        settings_store=SettingsStore(data),
        metadata_store=MetadataStore(data / "db.sqlite"),
    )
    launch = ManagedLaunch(
        launch_id="bad-path", script_path="x.bat", script_hash="h",
        identity=ProcessIdentity(pid=1, create_time=1), log_path="x.log",
        state=RuntimeState.FAILED, temporary_script_path=str(outside),
    )
    service.cleanup_temporary(launch)
    assert outside.exists()


def test_failed_override_write_leaves_no_partial_temporary_file(
    tmp_path: Path, monkeypatch,
) -> None:
    script = tmp_path / "run.bat"
    data = b"llama-server.exe -m x.gguf --port 8080\r\n"
    script.write_bytes(data)
    parsed = parse_script_bytes(data, script)
    output = tmp_path / "temporary"
    original_write = Path.write_bytes

    def failing_write(path: Path, value: bytes):
        if path.parent == output:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                handle.write(value[:5])
            raise OSError("simulated disk failure")
        return original_write(path, value)

    monkeypatch.setattr(Path, "write_bytes", failing_write)
    with pytest.raises(OSError, match="simulated"):
        generate_port_override(parsed, data, 8081, output)
    assert list(output.iterdir()) == []


def test_corrupt_database_is_quarantined_and_application_recovers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "llmbatdesk.db"
    database.write_bytes(b"not a sqlite database")
    store = MetadataStore(database)
    assert store.history_count() == 0
    assert list(tmp_path.glob("llmbatdesk.db.corrupt-*"))


def test_malformed_persisted_launch_is_ignored(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "db.sqlite")
    with store.connection() as db:
        db.execute(
            "INSERT INTO managed_launch(launch_id,data_json) VALUES (?,?)",
            ("bad", json.dumps({"launch_id": "bad"})),
        )
    assert store.load_launches() == []


def test_explicit_executable_paths_must_not_fall_back_to_basename() -> None:
    assert not _same_executable(
        r"C:\LLM-A\llama-server.exe", r"D:\LLM-B\llama-server.exe"
    )
    assert _same_executable("llama-server.exe", r"D:\LLM-B\llama-server.exe")


def test_graceful_process_group_request_precedes_termination() -> None:
    parent = ProcessIdentity(pid=10, create_time=10, executable="cmd.exe")

    class Inspector:
        graceful: list[int] = []
        terminated: list[int] = []
        alive = True

        def identity(self, pid):
            return parent if pid == 10 and self.alive else None

        def children(self, _pid):
            return []

        def request_graceful(self, identity):
            self.graceful.append(identity.pid)
            return True

        def wait(self, _identity, _timeout):
            self.alive = False
            return True

        def terminate(self, identity, force=False):
            self.terminated.append(identity.pid)
            return True

    inspector = Inspector()
    launch = ManagedLaunch(
        launch_id="graceful", script_path="x.bat", script_hash="h",
        identity=parent, log_path="x.log",
    )
    assert ProcessTracker(inspector).stop(launch, timeout=0.1)
    assert inspector.graceful == [10]
    assert inspector.terminated == []


def test_concurrent_launch_requests_are_deduplicated_before_port_check(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    script = tmp_path / "run.bat"
    executable.write_bytes(b"x")
    model.write_bytes(b"x")
    script.write_text(
        f'"{executable}" -m "{model}" --port 8080', encoding="utf-8"
    )
    record = ScriptScanner().scan([], [script])[0]
    first_in_check = threading.Event()
    release_check = threading.Event()

    class Ports:
        calls = 0

        def inspect(self, _port):
            self.calls += 1
            if self.calls == 1:
                first_in_check.set()
                assert release_check.wait(2)
            return None

    class Executor:
        calls = 0

        def start(self, *_args):
            self.calls += 1
            return ProcessIdentity(
                pid=100 + self.calls, create_time=float(100 + self.calls),
                executable="cmd.exe",
            )

    class Processes:
        def identity(self, _pid):
            return None

        def children(self, _pid):
            return []

    ports, executor = Ports(), Executor()
    service = ApplicationService(
        data_dir=tmp_path / "data",
        settings_store=SettingsStore(tmp_path / "data"),
        metadata_store=MetadataStore(tmp_path / "data" / "db.sqlite"),
        executor=executor,
        process_inspector=Processes(),
        port_inspector=ports,
    )

    def start():
        try:
            return service.launch(record, confirmed=True)
        except LaunchInProgressError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(start)
        assert first_in_check.wait(2)
        second = pool.submit(start)
        second.result(timeout=2)
        release_check.set()
        first.result(timeout=2)
    assert executor.calls == 1
