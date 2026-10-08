from __future__ import annotations

from pathlib import Path
from typing import BinaryIO, Protocol

from ..domain.models import ApiCheckResult, Backend, LaunchMode, PortOccupant, ProcessIdentity


class FileOperations(Protocol):
    def read_bytes(self, path: Path) -> bytes: ...
    def write_bytes(self, path: Path, data: bytes) -> None: ...


class RecycleBinOperations(Protocol):
    def move_to_trash(self, path: Path) -> None: ...


class PortInspection(Protocol):
    def inspect(self, port: int) -> PortOccupant | None: ...
    def find_free(self, start: int, host: str = "127.0.0.1") -> int: ...


class ProcessInspection(Protocol):
    def identity(self, pid: int) -> ProcessIdentity | None: ...
    def children(self, pid: int) -> list[ProcessIdentity]: ...
    def terminate(self, identity: ProcessIdentity, force: bool = False) -> bool: ...
    def wait(self, identity: ProcessIdentity, timeout: float) -> bool: ...


class ProcessExecution(Protocol):
    def start(
        self, script: Path, cwd: Path, stdout: BinaryIO | None, stderr: BinaryIO | None,
        launch_mode: LaunchMode = LaunchMode.BACKGROUND,
    ) -> ProcessIdentity: ...


class NetworkChecks(Protocol):
    def check(self, base_address: str, backend: Backend, api_key: str | None = None) -> ApiCheckResult: ...
