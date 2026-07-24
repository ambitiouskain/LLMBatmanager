from __future__ import annotations

import socket

import psutil

from ..domain.models import PortOccupant


class PsutilPortInspector:
    def __init__(self, managed_ports: dict[int, str] | None = None) -> None:
        self.managed_ports = managed_ports or {}

    def inspect(self, port: int) -> PortOccupant | None:
        try:
            connections = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, OSError):
            connections = []
        for connection in connections:
            if connection.laddr and connection.laddr.port == port and connection.status == "LISTEN":
                pid = connection.pid
                name = executable = None
                if pid:
                    try:
                        process = psutil.Process(pid)
                        name, executable = process.name(), process.exe()
                    except (psutil.Error, OSError):
                        pass
                return PortOccupant(
                    port=port, pid=pid, process_name=name, executable=executable,
                    managed_launch_id=self.managed_ports.get(port),
                )
        return None

    def find_free(self, start: int, host: str = "127.0.0.1") -> int:
        for port in range(max(1, start), 65536):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind((host, port))
                except OSError:
                    continue
                return port
        raise RuntimeError("没有可用 TCP 端口")


class ConflictDecision:
    STOP_AND_START = "stop_and_start"
    STOP_ONLY = "stop_only"
    ALTERNATE_PORT = "alternate_port"
    CANCEL = "cancel"


class PortConflictError(RuntimeError):
    def __init__(self, occupant: PortOccupant) -> None:
        super().__init__(f"端口 {occupant.port} 已被 PID {occupant.pid or '未知'} 占用")
        self.occupant = occupant


def conflict_choices(occupant: PortOccupant) -> tuple[str, ...]:
    if occupant.managed_launch_id:
        return (
            ConflictDecision.STOP_AND_START,
            ConflictDecision.STOP_ONLY,
            ConflictDecision.ALTERNATE_PORT,
            ConflictDecision.CANCEL,
        )
    return (ConflictDecision.ALTERNATE_PORT, ConflictDecision.CANCEL, "process_info")
