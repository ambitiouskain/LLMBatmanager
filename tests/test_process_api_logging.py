from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx

from llmbatdesk.domain.models import (
    Backend, ManagedLaunch, ProcessIdentity, RuntimeState,
)
from llmbatdesk.runtime.api import HttpxNetworkChecker, poll_readiness
from llmbatdesk.runtime.logging import OperationLogger, detect_failure, redact
from llmbatdesk.runtime.processes import ProcessTracker, identity_matches


class FakeProcessInspector:
    def __init__(self, identities: dict[int, ProcessIdentity], children: list[ProcessIdentity] | None = None):
        self.identities = identities
        self.child_list = children or []
        self.calls: list[tuple[int, bool]] = []
        self.wait_results = [False, True]

    def identity(self, pid: int):
        return self.identities.get(pid)

    def children(self, _pid: int):
        return self.child_list

    def terminate(self, identity: ProcessIdentity, force: bool = False):
        self.calls.append((identity.pid, force))
        return True

    def wait(self, _identity: ProcessIdentity, _timeout: float):
        return self.wait_results.pop(0) if self.wait_results else True


def launch(identity: ProcessIdentity, children: list[ProcessIdentity] | None = None) -> ManagedLaunch:
    return ManagedLaunch(
        launch_id="x", script_path="x.bat", script_hash="h", identity=identity,
        child_identities=children or [], log_path="x.log", started_at=datetime.now(),
    )


def test_process_ownership_verification_and_pid_reuse_rejection() -> None:
    expected = ProcessIdentity(pid=10, create_time=100.0, executable="cmd.exe", command_line=["cmd", "x"])
    assert identity_matches(expected, expected)
    reused = expected.model_copy(update={"create_time": 200.0})
    assert not identity_matches(expected, reused)


def test_refusal_to_kill_unrelated_process() -> None:
    expected = ProcessIdentity(pid=10, create_time=100)
    inspector = FakeProcessInspector({10: ProcessIdentity(pid=10, create_time=101)})
    assert not ProcessTracker(inspector).stop(launch(expected))
    assert inspector.calls == []


def test_graceful_stop_fallback_and_process_tree() -> None:
    parent = ProcessIdentity(pid=10, create_time=100)
    child = ProcessIdentity(pid=11, create_time=101)
    inspector = FakeProcessInspector({10: parent, 11: child}, [child])
    assert ProcessTracker(inspector).stop(launch(parent, [child]), timeout=0.01)
    assert (11, False) in inspector.calls and (10, False) in inspector.calls
    assert (10, True) in inspector.calls


def test_safe_reattachment() -> None:
    identity = ProcessIdentity(pid=10, create_time=100, executable="cmd.exe", command_line=["cmd"])
    inspector = FakeProcessInspector({10: identity})
    assert ProcessTracker(inspector).verify_reattachment(launch(identity))


def checker(response_status: int, body: dict | None = None) -> HttpxNetworkChecker:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(response_status, json=body or {}, request=request)
    )
    return HttpxNetworkChecker(httpx.Client(transport=transport))


def test_openai_models_endpoint_and_success() -> None:
    result = checker(200, {"data": [{"id": "model"}]}).check(
        "http://127.0.0.1:8080/v1", Backend.LLAMA_CPP
    )
    assert result.ready and result.loaded_models == ["model"]


def test_ollama_tags_endpoint() -> None:
    seen: list[str] = []
    transport = httpx.MockTransport(lambda request: (
        seen.append(request.url.path) or httpx.Response(200, json={"models": [{"name": "qwen"}]})
    ))
    result = HttpxNetworkChecker(httpx.Client(transport=transport)).check(
        "http://127.0.0.1:11434", Backend.OLLAMA
    )
    assert seen == ["/api/tags"] and result.loaded_models == ["qwen"]


def test_authentication_required_response() -> None:
    result = checker(401).check("http://localhost:8080/v1", Backend.LLAMA_CPP)
    assert result.state == RuntimeState.AUTH_REQUIRED


def test_api_timeout_with_fake_checker() -> None:
    class Never:
        def check(self, *_args):
            from llmbatdesk.domain.models import ApiCheckResult
            return ApiCheckResult(ready=False, state=RuntimeState.RUNNING_NOT_READY)
    result = poll_readiness(Never(), "x", Backend.LLAMA_CPP, timeout=0.001, interval=0.001)
    assert not result.ready and "超时" in result.detail


def test_process_exit_during_readiness() -> None:
    result = poll_readiness(checker(200), "x", Backend.LLAMA_CPP, timeout=1, process_alive=lambda: False)
    assert result.state == RuntimeState.FAILED


def test_log_creation_redaction_and_failure_detection(tmp_path: Path) -> None:
    logger = OperationLogger(tmp_path)
    path = logger.create({"command": "--api-key secret-value", "script": "中文.bat"})
    logger.append(path, "stderr", "CUDA out of memory\nAuthorization: Bearer another-secret\n")
    text = path.read_text(encoding="utf-8")
    assert "secret-value" not in text and "another-secret" not in text
    assert detect_failure(text) == "显存不足"
    assert "<已隐藏>" in text


def test_redact_assignment() -> None:
    assert "abc" not in redact("API_KEY=abc")

