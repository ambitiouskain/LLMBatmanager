from __future__ import annotations

import os
import difflib
import inspect
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .discovery import ScriptScanner, canonical_path
from .domain.models import (
    ACTIVE_RUNTIME_STATES, ApiCheckResult, ApiReadinessStatus, LaunchConfirmationMode,
    LaunchHistoryItem, LaunchMode, LaunchModeOverride, ManagedLaunch, Metadata,
    OperationEvent, ParsedScript, ParseConfidence, RunnabilityStatus, RuntimeState, ScriptRecord,
)
from .runtime.api import HttpxNetworkChecker, poll_readiness
from .runtime.cleanup import (
    CleanupResult, cleanup_logs, cleanup_temporary_files, delete_user_data_tree,
    storage_stats,
)
from .runtime.logging import OperationLogger
from .runtime.override import OverrideResult, generate_port_override
from .runtime.ports import ConflictDecision, PortConflictError, PsutilPortInspector
from .runtime.lifecycle import RuntimeReconciler
from .runtime.processes import ProcessTracker, PsutilProcessInspector, SubprocessExecutor
from .runtime.state_machine import transition
from .runnability import validate_runnability
from .settings import (
    AppSettings, SettingsStore, default_data_dir, resolve_editor_executable,
)
from .storage import MetadataStore


class LaunchBlockedError(RuntimeError):
    pass


class LaunchInProgressError(RuntimeError):
    pass


class ApplicationService:
    """GUI-facing orchestration. GUI code performs no parsing or process manipulation."""

    def __init__(
        self,
        data_dir: Path | None = None,
        settings_store: SettingsStore | None = None,
        metadata_store: MetadataStore | None = None,
        scanner: ScriptScanner | None = None,
        executor: object | None = None,
        process_inspector: object | None = None,
        port_inspector: object | None = None,
        network_checker: object | None = None,
    ) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.settings_store = settings_store or SettingsStore(self.data_dir)
        self.settings = self.settings_store.load()
        self.store = metadata_store or MetadataStore(self.data_dir / "llmbatdesk.db")
        self.scanner = scanner or ScriptScanner(self.settings.exclusions)
        self.executor = executor or SubprocessExecutor()
        self.process_inspector = process_inspector or PsutilProcessInspector()
        self.tracker = ProcessTracker(self.process_inspector)
        self.port_inspector = port_inspector or PsutilPortInspector()
        self.reconciler = RuntimeReconciler(self.process_inspector, self.port_inspector)
        self.network = network_checker or HttpxNetworkChecker()
        self.logger = OperationLogger(self.data_dir / "logs")
        self.records: dict[str, ScriptRecord] = {}
        self.launches: dict[str, ManagedLaunch] = {
            item.launch_id: item for item in self.store.load_launches()
        }
        self._launching_scripts: set[str] = set()
        self._reconcile_persisted()

    def _reconcile_persisted(self) -> None:
        for launch in self.launches.values():
            self.reconciler.reconcile(launch, startup=True)
            self.store.save_launch(launch)
            self.store.add_event(OperationEvent(kind="reattachment", data={
                "launch_id": launch.launch_id,
                "verified": launch.verified,
                "state": launch.state.value,
                "reason": launch.failure_reason,
                "pid": launch.server_identity.pid if launch.server_identity else None,
                "actual_port": launch.actual_port,
            }))
            if launch.state == RuntimeState.STALE_RECORD:
                self.store.add_event(OperationEvent(kind="stale_record", data={
                    "launch_id": launch.launch_id,
                    "script_path": launch.script_path,
                    "reason": launch.failure_reason,
                }))

    def active_launches(self) -> dict[str, ManagedLaunch]:
        return {
            launch_id: launch for launch_id, launch in self.launches.items()
            if launch.verified and launch.state in ACTIVE_RUNTIME_STATES
        }

    def pending_launches(self) -> dict[str, ManagedLaunch]:
        return {
            launch_id: launch for launch_id, launch in self.launches.items()
            if launch.state == RuntimeState.STARTING and not launch.verified
        }

    def history_launches(self) -> dict[str, ManagedLaunch]:
        active_ids = self.active_launches().keys()
        return {
            launch_id: launch for launch_id, launch in self.launches.items()
            if launch_id not in active_ids
        }

    def history_entries(self) -> list[LaunchHistoryItem]:
        entries = [
            LaunchHistoryItem(
                history_id=launch.launch_id,
                script_path=launch.script_path,
                configured_port=launch.configured_port,
                actual_port=launch.actual_port,
                state=launch.state.value,
                reason=launch.failure_reason,
                timestamp=launch.started_at,
            )
            for launch in self.history_launches().values()
        ]
        events = self.store.list_events(
            {"launch_blocked", "launch_failed_before_tracking"}, limit=500
        )
        for index, event in enumerate(events):
            entries.append(LaunchHistoryItem(
                history_id=f"event-{event.timestamp.timestamp()}-{index}",
                script_path=str(event.data.get("script_path", "")),
                model_name=str(event.data.get("model", "")),
                configured_port=event.data.get("configured_port"),
                actual_port=event.data.get("actual_port"),
                state=str(event.data.get("state", "启动已阻止")),
                reason=str(event.data.get("reason", "")),
                timestamp=event.timestamp,
            ))
        return sorted(entries, key=lambda item: item.timestamp, reverse=True)

    def reconcile_runtimes(self) -> dict[str, ManagedLaunch]:
        before = set(self.active_launches())
        for launch in self.launches.values():
            if launch.state in {
                RuntimeState.STOPPED, RuntimeState.FAILED, RuntimeState.STALE_RECORD
            }:
                continue
            record = next(
                (item for item in self.records.values()
                 if str(item.parsed.path) == launch.script_path),
                None,
            )
            previous = launch.state
            self.reconciler.reconcile(launch, record.parsed if record else None)
            self.store.save_launch(launch)
            if launch.state != previous:
                self.store.add_event(OperationEvent(kind="runtime_state", data={
                    "launch_id": launch.launch_id,
                    "from": previous.value,
                    "to": launch.state.value,
                    "verified": launch.verified,
                    "reason": launch.failure_reason,
                }))
                if launch.state in {RuntimeState.FAILED, RuntimeState.STALE_RECORD}:
                    self.cleanup_temporary(launch)
                    self.cleanup_expired_logs()
        after = set(self.active_launches())
        for launch_id in after - before:
            self.store.add_event(OperationEvent(kind="active_row_insert", data={"launch_id": launch_id}))
        for launch_id in before - after:
            self.store.add_event(OperationEvent(kind="active_row_remove", data={"launch_id": launch_id}))
        return self.active_launches()

    def rescan(self) -> list[ScriptRecord]:
        scanner = ScriptScanner(self.settings.exclusions)
        records = scanner.scan(
            (Path(root) for root in self.settings.roots),
            (Path(path) for path in self.settings.individual_scripts),
        )
        self.records = {record.fingerprint.canonical_path: record for record in records}
        return records

    def add_root(self, path: Path) -> None:
        value = str(path.resolve())
        if value.casefold() not in {root.casefold() for root in self.settings.roots}:
            self.settings.roots.append(value)
            self.settings_store.save(self.settings)

    def add_individual(self, path: Path) -> ScriptRecord:
        records = self.scanner.scan([], [path])
        if not records:
            raise ValueError("请选择存在的 .bat 或 .cmd 文件")
        record = records[0]
        value = str(path.resolve())
        if value.casefold() not in {item.casefold() for item in self.settings.individual_scripts}:
            self.settings.individual_scripts.append(value)
            self.settings_store.save(self.settings)
        self.records[record.fingerprint.canonical_path] = record
        return record

    def metadata(self, record: ScriptRecord) -> Metadata:
        return self.store.get_metadata(record.fingerprint.canonical_path)

    def save_metadata(self, metadata: Metadata) -> None:
        self.store.save_metadata(metadata)

    def is_trusted(self, record: ScriptRecord, working_directory: Path | None = None) -> bool:
        directory = str((working_directory or record.parsed.path.parent).resolve())
        return self.store.is_trusted(record.fingerprint.sha256, directory)

    def trust(self, record: ScriptRecord, working_directory: Path | None = None) -> None:
        directory = str((working_directory or record.parsed.path.parent).resolve())
        self.store.trust(
            record.fingerprint.sha256, directory, record.fingerprint.canonical_path
        )

    def effective_launch_mode(self, record: ScriptRecord) -> LaunchMode:
        override = self.metadata(record).launch_mode_override
        if override == LaunchModeOverride.BACKGROUND:
            return LaunchMode.BACKGROUND
        if override == LaunchModeOverride.VISIBLE:
            return LaunchMode.VISIBLE
        return self.settings.launch_mode

    def safety_confirmation_reasons(
        self,
        record: ScriptRecord,
        *,
        override: OverrideResult | None = None,
        working_directory: Path | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        directory = (working_directory or record.parsed.path.parent).resolve()
        trust_reason = self.store.trust_reason(
            record.fingerprint.sha256,
            str(directory),
            record.fingerprint.canonical_path,
        )
        if trust_reason:
            reasons.append(trust_reason)
        if record.parsed.confidence != ParseConfidence.FULL:
            reasons.append("脚本包含部分或动态解析行为")
        if override is not None:
            reasons.append("临时端口覆盖")
        if record.parsed.interactive_commands and (
            self.effective_launch_mode(record) == LaunchMode.BACKGROUND
        ):
            reasons.append(
                "检测到需要输入的交互命令："
                + "、".join(record.parsed.interactive_commands)
                + "；后台模式可能等待隐藏输入"
            )
        if record.runnability.resolved_executable is None:
            reasons.append("可执行文件路径尚未可靠解析")
        return list(dict.fromkeys(reasons))

    def should_confirm_launch(
        self, record: ScriptRecord, *, override: OverrideResult | None = None
    ) -> bool:
        required = self.safety_confirmation_reasons(record, override=override)
        if required:
            return True
        return self.settings.launch_confirmation_mode == LaunchConfirmationMode.ALWAYS

    def inspect_port(self, parsed: ParsedScript, actual_port: int | None = None) -> object | None:
        self.reconcile_runtimes()
        port = actual_port or parsed.configured_port
        occupant = self.port_inspector.inspect(port) if port else None
        if occupant and occupant.pid:
            for launch in self.active_launches().values():
                owned_pids = {
                    *(item.pid for item in launch.child_identities),
                    *([launch.server_identity.pid] if launch.server_identity else []),
                }
                if occupant.pid in owned_pids and launch.actual_port == port:
                    occupant.managed_launch_id = launch.launch_id
                    break
            self.store.add_event(OperationEvent(kind="port_conflict_check", data={
                "port": port,
                "listening_pid": occupant.pid,
                "classification": "managed" if occupant.managed_launch_id else "unmanaged",
            }))
        return occupant

    def prepare_override(self, record: ScriptRecord, new_port: int) -> OverrideResult:
        try:
            result = generate_port_override(
                record.parsed, Path(record.parsed.path).read_bytes(), new_port,
                self.data_dir / "temporary",
            )
        except Exception as error:
            self.store.add_event(OperationEvent(kind="port_override_refused", data={
                "script_path": str(record.parsed.path),
                "configured_port": record.parsed.configured_port,
                "requested_port": new_port,
                "reason": str(error),
            }))
            raise
        self.store.add_event(OperationEvent(kind="port_override_allowed", data={
            "script_path": str(record.parsed.path),
            "configured_port": record.parsed.configured_port,
            "actual_port": new_port,
            "temporary_path": str(result.path),
        }))
        return result

    def handle_conflict_decision(self, decision: str, occupant: object) -> str:
        if decision == ConflictDecision.CANCEL:
            return "cancel"
        if decision == ConflictDecision.ALTERNATE_PORT:
            return "alternate"
        if decision in {ConflictDecision.STOP_AND_START, ConflictDecision.STOP_ONLY}:
            if not occupant.managed_launch_id:
                raise PermissionError("不会停止非 LLMBatDesk 托管的端口占用进程")
            if not self.stop(occupant.managed_launch_id):
                raise RuntimeError("无法验证或停止当前托管服务器")
            return "start" if decision == ConflictDecision.STOP_AND_START else "stopped"
        if decision == "process_info":
            return "process_info"
        raise ValueError(f"未知端口冲突决策：{decision}")

    def launch(
        self, record: ScriptRecord, override: OverrideResult | None = None,
        *, confirmed: bool = False,
    ) -> ManagedLaunch:
        if not self.is_trusted(record) and not confirmed:
            raise PermissionError("脚本当前内容尚未获得信任确认")
        if record.runnability.status == RunnabilityStatus.NOT_VALIDATED:
            record.runnability = validate_runnability(record.parsed)
        if not record.runnability.launch_allowed:
            reason = "\n\n".join(record.runnability.warnings) or record.runnability.status.value
            self.store.add_event(OperationEvent(kind="launch_blocked", data={
                "script_path": str(record.parsed.path),
                "model": record.parsed.model_name or "",
                "configured_port": record.parsed.configured_port,
                "state": "启动已阻止",
                "reason": reason,
            }))
            raise LaunchBlockedError(reason)
        script_key = record.fingerprint.canonical_path
        if script_key in self._launching_scripts or any(
            item.script_path == str(record.parsed.path) and item.state == RuntimeState.STARTING
            for item in self.launches.values()
        ):
            self.store.add_event(OperationEvent(kind="launch_deduplicated", data={
                "script_path": str(record.parsed.path),
            }))
            raise LaunchInProgressError("该脚本已有启动请求正在等待验证")
        script = override.path if override else record.parsed.path
        actual_port = override.new_port if override else record.parsed.configured_port
        occupant = self.inspect_port(record.parsed, actual_port)
        if occupant:
            raise PortConflictError(occupant)
        self._launching_scripts.add(script_key)
        try:
            launch_mode = self.effective_launch_mode(record)
            log_path = self.logger.create({
                "原始脚本": record.parsed.path,
                "原始哈希": record.fingerprint.sha256,
                "临时脚本": override.path if override else "",
                "工作目录": record.parsed.path.parent,
                "后端": record.parsed.backend.value,
                "模型": record.parsed.model_name or "",
                "配置端口": record.parsed.configured_port,
                "实际端口": actual_port,
                "冲突检测": "已执行；未发现监听占用",
                "解析警告": "; ".join(record.parsed.warnings),
                "启动机制": 'cmd.exe /d /s /c call "<script>"（shell=False）',
                "控制台模式": launch_mode.value,
            })
            output = (
                log_path.open("ab", buffering=0)
                if launch_mode == LaunchMode.BACKGROUND else None
            )
            try:
                parameters = inspect.signature(self.executor.start).parameters
                arguments = (
                    (script, record.parsed.path.parent, output, output, launch_mode)
                    if "launch_mode" in parameters
                    else (script, record.parsed.path.parent, output, output)
                )
                identity = self.executor.start(*arguments)
            finally:
                if output is not None:
                    output.close()
        except Exception as error:
            self.store.add_event(OperationEvent(kind="launch_failed_before_tracking", data={
                "script_path": str(record.parsed.path),
                "model": record.parsed.model_name or "",
                "configured_port": record.parsed.configured_port,
                "actual_port": actual_port,
                "state": RuntimeState.FAILED.value,
                "reason": str(error),
            }))
            raise
        finally:
            self._launching_scripts.discard(script_key)
        identity.port = actual_port
        launch_id = uuid4().hex
        launch = ManagedLaunch(
            launch_id=launch_id, script_path=str(record.parsed.path),
            script_hash=record.fingerprint.sha256, identity=identity,
            child_identities=self.process_inspector.children(identity.pid),
            configured_port=record.parsed.configured_port, actual_port=actual_port,
            log_path=str(log_path),
            temporary_script_path=str(override.path) if override else None,
        )
        self.launches[launch.launch_id] = launch
        self.reconciler.reconcile(launch, record.parsed)
        self.store.save_launch(launch)
        metadata = self.metadata(record)
        metadata.last_run_at = datetime.now()
        self.save_metadata(metadata)
        self.store.add_event(OperationEvent(kind="launch", data={
            "launch_id": launch.launch_id, "script_path": launch.script_path,
            "configured_port": launch.configured_port, "actual_port": actual_port,
            "verified": launch.verified, "state": launch.state.value,
            "launch_mode": self.effective_launch_mode(record).value,
        }))
        return launch

    def stop(self, launch_id: str) -> bool:
        launch = self.launches[launch_id]
        self.reconciler.reconcile(launch)
        if launch_id not in self.active_launches():
            return False
        launch.state = transition(launch.state, RuntimeState.STOPPING)
        stopped = self.tracker.stop(launch, self.settings.stop_timeout_seconds)
        launch.user_stopped = True
        launch.state = (
            transition(launch.state, RuntimeState.STOPPED) if stopped
            else transition(launch.state, RuntimeState.DETACHED_UNVERIFIED)
        )
        launch.verified = False
        self.store.save_launch(launch)
        self.cleanup_temporary(launch)
        self.cleanup_expired_logs()
        return stopped

    def restart(self, launch_id: str, record: ScriptRecord) -> ManagedLaunch:
        if not self.stop(launch_id):
            raise RuntimeError("无法验证或停止原进程，已取消重启")
        return self.launch(record)

    def begin_api_check(self, launch: ManagedLaunch) -> int:
        launch.api_check_generation += 1
        launch.api_status = ApiReadinessStatus.PENDING
        launch.api_last_error = ""
        self.store.save_launch(launch)
        return launch.api_check_generation

    def apply_api_result(
        self, launch: ManagedLaunch, result: ApiCheckResult, generation: int
    ) -> bool:
        if generation != launch.api_check_generation:
            self.store.add_event(OperationEvent(kind="api_stale_result_ignored", data={
                "launch_id": launch.launch_id,
                "generation": generation,
                "current_generation": launch.api_check_generation,
            }))
            return False
        if (
            launch.api_last_check_at is not None
            and result.checked_at < launch.api_last_check_at
        ):
            return False
        previous = launch.state
        launch.api_checked_url = result.checked_url
        launch.api_last_http_status = result.status_code
        launch.api_last_error = "" if result.ready else result.detail
        launch.api_last_check_at = result.checked_at
        launch.api_retry_count = max(launch.api_retry_count, result.retry_count)
        status_map = {
            "": ApiReadinessStatus.READY if result.ready else ApiReadinessStatus.ERROR,
            "authentication_required": ApiReadinessStatus.AUTH_REQUIRED,
            "incompatible_endpoint": ApiReadinessStatus.INCOMPATIBLE,
            "timeout": ApiReadinessStatus.TIMED_OUT,
            "startup_timeout": ApiReadinessStatus.TIMED_OUT,
            "process_exited": ApiReadinessStatus.PROCESS_EXITED,
            "request_error": ApiReadinessStatus.ERROR,
            "http_error": ApiReadinessStatus.ERROR,
        }
        launch.api_status = status_map.get(result.error_kind, ApiReadinessStatus.ERROR)
        launch.api_ready = result.ready
        target_state = result.state
        if result.error_kind == "process_exited":
            target_state = RuntimeState.FAILED
            launch.verified = False
        elif (
            not result.ready
            and launch.state == RuntimeState.PROCESS_RUNNING_PORT_CLOSED
        ):
            target_state = RuntimeState.PROCESS_RUNNING_PORT_CLOSED
        launch.state = self.reconciler._safe_transition(launch.state, target_state)
        self.store.save_launch(launch)
        self.store.add_event(OperationEvent(kind="api_state", data={
            "launch_id": launch.launch_id,
            "from": previous.value,
            "to": launch.state.value,
            "ready": result.ready,
            "status_code": result.status_code,
            "checked_url": result.checked_url,
            "error": result.detail if not result.ready else "",
            "retry_count": result.retry_count,
        }))
        return True

    def check_api(self, launch: ManagedLaunch, parsed: ParsedScript) -> ApiCheckResult:
        from .parsing.parser import runtime_api_address
        if launch.actual_port is None:
            raise ValueError("没有可检查的静态端口")
        self.reconciler.reconcile(launch, parsed)
        if launch.launch_id not in self.active_launches():
            raise RuntimeError("没有可验证的活动服务器可供 API 检查")
        generation = self.begin_api_check(launch)
        result = self.network.check(
            runtime_api_address(parsed, launch.actual_port), parsed.backend
        )
        result.retry_count = 1
        self.apply_api_result(launch, result, generation)
        return result

    def poll_api_readiness(
        self, launch: ManagedLaunch, parsed: ParsedScript
    ) -> ApiCheckResult:
        from .parsing.parser import runtime_api_address
        if launch.actual_port is None:
            raise ValueError("没有可检查的静态端口")
        generation = self.begin_api_check(launch)

        def alive() -> bool:
            self.reconciler.reconcile(launch, parsed)
            self.store.save_launch(launch)
            return launch.state not in {
                RuntimeState.FAILED, RuntimeState.STALE_RECORD, RuntimeState.STOPPED
            }

        def apply(result: ApiCheckResult) -> None:
            self.apply_api_result(launch, result, generation)

        result = poll_readiness(
            self.network,
            runtime_api_address(parsed, launch.actual_port),
            parsed.backend,
            timeout=self.settings.api_startup_timeout_seconds,
            interval=1.0,
            process_alive=alive,
            on_result=apply,
        )
        if launch.api_last_check_at != result.checked_at:
            self.apply_api_result(launch, result, generation)
        return result

    def leave_running(self) -> None:
        self.reconcile_runtimes()
        for launch in self.active_launches().values():
            if launch.verified:
                self.store.save_launch(launch)

    def cleanup_temporary(self, launch: ManagedLaunch) -> None:
        if launch.temporary_script_path and launch.state in {
            RuntimeState.STOPPED, RuntimeState.FAILED
        }:
            Path(launch.temporary_script_path).unlink(missing_ok=True)

    def storage_stats(self):
        return storage_stats(
            self.data_dir, self.store.path, self.store.history_count()
        )

    def cleanup_expired_logs(self) -> CleanupResult:
        protected = {
            Path(item.log_path) for item in self.active_launches().values()
            if item.log_path
        }
        return cleanup_logs(
            self.data_dir / "logs",
            retention_days=self.settings.log_retention_days,
            max_total_bytes=(
                self.settings.log_max_total_mb * 1024 * 1024
                if self.settings.log_max_total_mb is not None else None
            ),
            max_count=self.settings.log_max_count,
            protected_paths=protected,
        )

    def clear_all_logs(self) -> CleanupResult:
        protected = {
            Path(item.log_path) for item in self.active_launches().values()
            if item.log_path
        }
        return cleanup_logs(
            self.data_dir / "logs",
            retention_days=None,
            max_total_bytes=None,
            max_count=None,
            protected_paths=protected,
            delete_all=True,
        )

    def clear_temporary_files(self) -> CleanupResult:
        protected = {
            Path(item.temporary_script_path)
            for item in self.active_launches().values()
            if item.temporary_script_path
        }
        return cleanup_temporary_files(self.data_dir / "temporary", protected)

    def clear_launch_history(self) -> int:
        active_ids = set(self.active_launches())
        for launch_id in list(self.launches):
            if launch_id not in active_ids:
                self.store.remove_launch(launch_id)
                self.launches.pop(launch_id, None)
        return self.store.clear_history()

    def reset_ui_settings(self) -> None:
        defaults = AppSettings()
        for field in (
            "qt_geometry", "qt_window_state", "qt_splitter_state",
            "qt_selected_tab", "qt_runtime_tab", "qt_column_widths",
            "last_selected_path",
        ):
            setattr(self.settings, field, getattr(defaults, field))
        self.settings_store.save(self.settings)

    def delete_all_user_data(self, typed_confirmation: str) -> None:
        if typed_confirmation != "删除全部数据":
            raise PermissionError("请输入“删除全部数据”以确认")
        if self.active_launches() or self.pending_launches():
            raise RuntimeError("存在活动或正在启动的服务，不能删除全部用户数据")
        delete_user_data_tree(self.data_dir)
        self.settings_store = SettingsStore(self.data_dir)
        self.settings = AppSettings()
        self.store = MetadataStore(self.data_dir / "llmbatdesk.db")
        self.launches.clear()

    def duplicate_script(self, record: ScriptRecord, destination: Path, managed: bool = False) -> str:
        if destination.exists():
            raise FileExistsError(f"不会覆盖已有文件：{destination}")
        original = record.parsed.path.read_bytes()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if managed:
            from .parsing.tokenizer import decode_batch
            text, encoding = decode_batch(original)
            newline = "\r\n" if "\r\n" in text else "\n"
            output = (
                f"rem LLMBatDesk generated managed copy; source: {record.parsed.path}{newline}"
                + text
            ).encode(encoding)
            destination.write_bytes(output)
        else:
            shutil.copy2(record.parsed.path, destination)
            output = destination.read_bytes()
        return "".join(difflib.unified_diff(
            record.parsed.raw_text.splitlines(keepends=True),
            output.decode(record.parsed.encoding).splitlines(keepends=True),
            fromfile=str(record.parsed.path), tofile=str(destination),
        ))

    def save_temporary_as_managed(self, launch: ManagedLaunch, destination: Path) -> str:
        if not launch.temporary_script_path:
            raise ValueError("所选启动没有临时端口副本")
        if destination.exists():
            raise FileExistsError(f"不会覆盖已有文件：{destination}")
        original = Path(launch.script_path)
        temporary = Path(launch.temporary_script_path)
        from .parsing.tokenizer import decode_batch
        before, _ = decode_batch(original.read_bytes())
        after, encoding = decode_batch(temporary.read_bytes())
        newline = "\r\n" if "\r\n" in after else "\n"
        managed = f"rem LLMBatDesk generated managed copy; source: {original}{newline}" + after
        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), managed.splitlines(keepends=True),
            fromfile=str(original), tofile=str(destination),
        ))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(managed.encode(encoding))
        return diff

    def open_path(self, path: Path) -> None:
        if os.name != "nt":
            raise RuntimeError("此操作仅在 Windows 上可用")
        os.startfile(path)  # type: ignore[attr-defined]

    def open_script(self, path: Path) -> None:
        if self.settings.editor_mode.value == "system_default":
            if os.name != "nt":
                raise RuntimeError("Windows 默认编辑关联仅在 Windows 上可用")
            # The default "open" verb for BAT/CMD may execute the script.
            # The registered "edit" verb opens its system-associated editor.
            os.startfile(path, "edit")  # type: ignore[attr-defined]
            return
        editor = resolve_editor_executable(self.settings.editor_path)
        if editor is None:
            raise FileNotFoundError(
                f"指定的外部编辑器不存在：{self.settings.editor_path or '未选择'}"
            )
        subprocess.Popen(
            [str(editor), str(path.resolve())],
            shell=False,
            cwd=str(path.resolve().parent),
        )

    def display_name(self, record: ScriptRecord) -> str:
        metadata = self.metadata(record)
        return metadata.display_name or record.parsed.path.stem

    def search(self, query: str) -> list[ScriptRecord]:
        needle = query.casefold().strip()
        if not needle:
            return list(self.records.values())
        found = []
        for record in self.records.values():
            metadata = self.metadata(record)
            haystack = " ".join([
                str(record.parsed.path), record.parsed.model_name or "",
                record.parsed.backend.value, metadata.display_name, metadata.notes,
                metadata.category, " ".join(metadata.tags), " ".join(metadata.games),
            ]).casefold()
            if needle in haystack:
                found.append(record)
        return found
