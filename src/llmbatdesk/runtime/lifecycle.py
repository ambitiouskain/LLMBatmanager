from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from ..domain.models import ManagedLaunch, ParsedScript, RuntimeState
from .processes import identity_matches
from .state_machine import transition


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(launch: ManagedLaunch) -> float:
    started = launch.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0.0, (_utc_now() - started).total_seconds())


def _same_executable(actual: str | None, expected: str | None) -> bool:
    if not actual or not expected:
        return False
    actual_path, expected_path = Path(actual), Path(expected)
    try:
        if actual_path.resolve() == expected_path.resolve():
            return True
    except OSError:
        pass
    # When both sides provide explicit paths, a basename match is insufficient:
    # it could attach an unrelated binary with the same common filename.
    if actual_path.is_absolute() and expected_path.is_absolute():
        return False
    return actual_path.name.casefold() == expected_path.name.casefold()


class RuntimeReconciler:
    """Turns untrusted launch records into verified active services or history."""

    def __init__(self, process_inspector: object, port_inspector: object, startup_grace: float = 15.0):
        self.process_inspector = process_inspector
        self.port_inspector = port_inspector
        self.startup_grace = startup_grace

    def reconcile(
        self,
        launch: ManagedLaunch,
        parsed: ParsedScript | None = None,
        *,
        startup: bool = False,
    ) -> ManagedLaunch:
        launch.verified = False
        launcher = self.process_inspector.identity(launch.identity.pid)
        launcher_valid = identity_matches(launch.identity, launcher)
        current_children = self.process_inspector.children(launch.identity.pid) if launcher_valid else []
        saved_candidates = [
            item for item in [launch.server_identity, *launch.child_identities] if item is not None
        ]
        verified_saved = []
        for saved in saved_candidates:
            actual = self.process_inspector.identity(saved.pid)
            if identity_matches(saved, actual):
                verified_saved.append(actual)
        candidates = self._deduplicate([*verified_saved, *current_children])
        expected = parsed.executable if parsed else (
            launch.server_identity.executable if launch.server_identity else None
        )
        server_candidates = [
            item for item in candidates if _same_executable(item.executable, expected)
        ] if expected else []

        occupant = self.port_inspector.inspect(launch.actual_port) if launch.actual_port else None
        owned_pids = {item.pid for item in server_candidates}
        if occupant and occupant.pid in owned_pids:
            server = next(item for item in server_candidates if item.pid == occupant.pid)
            launch.server_identity = server
            launch.child_identities = self._deduplicate([*launch.child_identities, *candidates])
            launch.verified = True
            launch.last_verified_at = _utc_now()
            target = (
                RuntimeState.API_READY if launch.api_ready
                else RuntimeState.PORT_LISTENING_API_NOT_READY
            )
            launch.state = self._safe_transition(launch.state, target)
            return launch

        if occupant and occupant.pid not in owned_pids:
            launch.failure_reason = (
                f"端口 {launch.actual_port} 由其他 PID {occupant.pid or '未知'} 占用"
            )
            launch.state = self._terminal_state(launch, startup)
            return launch

        if server_candidates:
            server = server_candidates[0]
            launch.server_identity = server
            launch.child_identities = self._deduplicate([*launch.child_identities, *candidates])
            launch.verified = True
            launch.last_verified_at = _utc_now()
            launch.state = self._safe_transition(
                launch.state, RuntimeState.PROCESS_RUNNING_PORT_CLOSED
            )
            return launch

        # cmd.exe alone is intentionally not enough to create an active service row.
        if launcher_valid and not startup and _age_seconds(launch) < self.startup_grace:
            launch.state = RuntimeState.STARTING
            launch.failure_reason = "等待可验证的服务器子进程或监听端口"
            return launch

        launch.failure_reason = (
            "仅检测到启动器，未检测到可验证的服务器进程"
            if launcher_valid else "启动进程已退出，且未检测到可验证的服务器进程"
        )
        launch.state = self._terminal_state(launch, startup)
        return launch

    @staticmethod
    def _deduplicate(identities: list[object]) -> list[object]:
        result: dict[tuple[int, float], object] = {}
        for identity in identities:
            result[(identity.pid, identity.create_time)] = identity
        return list(result.values())

    @staticmethod
    def _safe_transition(current: RuntimeState, target: RuntimeState) -> RuntimeState:
        try:
            return transition(current, target)
        except ValueError:
            # Persisted legacy states must first earn verification; once verified, the
            # observed state is authoritative.
            return target

    @staticmethod
    def _terminal_state(launch: ManagedLaunch, startup: bool) -> RuntimeState:
        if startup:
            return RuntimeState.STALE_RECORD
        try:
            return transition(launch.state, RuntimeState.FAILED)
        except ValueError:
            return RuntimeState.FAILED
