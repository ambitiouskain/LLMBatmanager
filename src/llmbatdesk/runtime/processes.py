from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import BinaryIO

import psutil

from ..domain.models import ManagedLaunch, ProcessIdentity
from ..domain.models import LaunchMode


class SubprocessExecutor:
    def start(
        self, script: Path, cwd: Path, stdout: BinaryIO | None, stderr: BinaryIO | None,
        launch_mode: LaunchMode = LaunchMode.BACKGROUND,
    ) -> ProcessIdentity:
        if script.suffix.casefold() not in {".bat", ".cmd"}:
            raise ValueError("仅允许启动 BAT/CMD 脚本")
        resolved = script.resolve(strict=True)
        command = [os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
                   "/d", "/s", "/c", "call", str(resolved)]
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        startupinfo = None
        stdin: object = subprocess.DEVNULL
        if os.name == "nt" and launch_mode == LaunchMode.BACKGROUND:
            creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
        elif os.name == "nt" and launch_mode == LaunchMode.VISIBLE:
            creationflags |= getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            stdout = None
            stderr = None
            stdin = None
        process = subprocess.Popen(
            command, cwd=cwd, stdout=stdout, stderr=stderr, stdin=stdin,
            shell=False, creationflags=creationflags, startupinfo=startupinfo,
        )
        return ProcessIdentity(
            pid=process.pid, create_time=psutil.Process(process.pid).create_time(),
            executable=command[0], command_line=command,
        )


class PsutilProcessInspector:
    def identity(self, pid: int) -> ProcessIdentity | None:
        try:
            process = psutil.Process(pid)
            return ProcessIdentity(
                pid=pid, create_time=process.create_time(), executable=process.exe(),
                command_line=process.cmdline(),
            )
        except (psutil.Error, OSError):
            return None

    def children(self, pid: int) -> list[ProcessIdentity]:
        try:
            children = psutil.Process(pid).children(recursive=True)
        except psutil.Error:
            return []
        result: list[ProcessIdentity] = []
        for child in children:
            identity = self.identity(child.pid)
            if identity:
                result.append(identity)
        return result

    def terminate(self, identity: ProcessIdentity, force: bool = False) -> bool:
        current = self.identity(identity.pid)
        if not identity_matches(identity, current):
            return False
        try:
            process = psutil.Process(identity.pid)
            process.kill() if force else process.terminate()
            return True
        except psutil.Error:
            return False

    def request_graceful(self, identity: ProcessIdentity) -> bool:
        current = self.identity(identity.pid)
        if not identity_matches(identity, current):
            return False
        try:
            if os.name == "nt":
                os.kill(identity.pid, signal.CTRL_BREAK_EVENT)
            else:
                psutil.Process(identity.pid).terminate()
            return True
        except (OSError, psutil.Error):
            return False

    def wait(self, identity: ProcessIdentity, timeout: float) -> bool:
        current = self.identity(identity.pid)
        if not identity_matches(identity, current):
            return True
        try:
            psutil.Process(identity.pid).wait(timeout=timeout)
            return True
        except psutil.TimeoutExpired:
            return False
        except psutil.Error:
            return True


def identity_matches(expected: ProcessIdentity, actual: ProcessIdentity | None) -> bool:
    if actual is None or expected.pid != actual.pid:
        return False
    if abs(expected.create_time - actual.create_time) > 0.01:
        return False
    if expected.executable and actual.executable:
        if Path(expected.executable).resolve() != Path(actual.executable).resolve():
            return False
    if expected.command_line and actual.command_line:
        if expected.command_line != actual.command_line:
            return False
    if expected.port and actual.port and expected.port != actual.port:
        return False
    return True


class ProcessTracker:
    def __init__(self, inspector: object) -> None:
        self.inspector = inspector

    def verify_reattachment(self, launch: ManagedLaunch) -> bool:
        actual = self.inspector.identity(launch.identity.pid)
        return identity_matches(launch.identity, actual)

    def stop(self, launch: ManagedLaunch, timeout: float = 5.0) -> bool:
        parent_verified = self.verify_reattachment(launch)
        server_verified = (
            launch.server_identity is not None
            and identity_matches(
                launch.server_identity,
                self.inspector.identity(launch.server_identity.pid),
            )
        )
        if not parent_verified and not server_verified:
            return False
        children = self.inspector.children(launch.identity.pid) if parent_verified else []
        verified = children
        if server_verified and not any(
            identity_matches(launch.server_identity, child) for child in verified
        ):
            verified.append(launch.server_identity)
        graceful = getattr(self.inspector, "request_graceful", None)
        if parent_verified and callable(graceful) and graceful(launch.identity):
            if self.inspector.wait(launch.identity, timeout):
                still_owned = any(
                    identity_matches(
                        identity, self.inspector.identity(identity.pid)
                    )
                    for identity in verified
                )
                if not still_owned:
                    return True
        for identity in reversed(verified):
            self.inspector.terminate(identity, force=False)
        if parent_verified:
            self.inspector.terminate(launch.identity, force=False)
        wait_target = launch.identity if parent_verified else launch.server_identity
        if self.inspector.wait(wait_target, timeout):
            return True
        for identity in reversed(verified):
            self.inspector.terminate(identity, force=True)
        if parent_verified:
            self.inspector.terminate(launch.identity, force=True)
        return self.inspector.wait(wait_target, timeout)
