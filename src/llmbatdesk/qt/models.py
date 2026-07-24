from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from ..domain.models import (
    ApiReadinessStatus, LaunchHistoryItem, ManagedLaunch, RuntimeState, ScriptRecord,
)


def runtime_display_state(state: RuntimeState) -> str:
    if state == RuntimeState.STARTING:
        return "正在启动"
    if state in {
        RuntimeState.PROCESS_RUNNING_PORT_CLOSED,
        RuntimeState.PORT_LISTENING_API_NOT_READY,
        RuntimeState.API_READY,
        RuntimeState.AUTH_REQUIRED,
    }:
        return "运行中"
    return {
        RuntimeState.STOPPING: "正在停止",
        RuntimeState.STOPPED: "已停止",
        RuntimeState.FAILED: "启动失败",
        RuntimeState.DETACHED_UNVERIFIED: "已分离，无法验证",
        RuntimeState.STALE_RECORD: "已过期",
    }.get(state, state.value)


def api_display_state(launch: ManagedLaunch) -> str:
    if launch.state in {
        RuntimeState.FAILED, RuntimeState.STOPPED, RuntimeState.STOPPING,
        RuntimeState.DETACHED_UNVERIFIED, RuntimeState.STALE_RECORD,
    }:
        return "不适用"
    if launch.api_status == ApiReadinessStatus.PENDING:
        return "检查中"
    if launch.api_status == ApiReadinessStatus.READY or launch.api_ready:
        return "API 已就绪"
    if launch.api_status == ApiReadinessStatus.AUTH_REQUIRED:
        return "需要认证"
    if launch.api_status == ApiReadinessStatus.INCOMPATIBLE:
        return "端点不兼容"
    if launch.api_status == ApiReadinessStatus.TIMED_OUT:
        return "请求超时"
    if launch.api_status == ApiReadinessStatus.ERROR:
        return "请求失败"
    if launch.state == RuntimeState.PROCESS_RUNNING_PORT_CLOSED:
        return "端口尚未监听"
    if launch.state == RuntimeState.PORT_LISTENING_API_NOT_READY:
        return "API 未就绪"
    return "未检查"


class LaunchTableModel(QAbstractTableModel):
    ACTIVE_HEADERS = ("名称", "模型", "配置端口", "实际端口", "PID", "状态", "API", "最后验证")
    HISTORY_HEADERS = ("名称", "模型", "端口", "结果", "说明", "时间")

    def __init__(self, active: bool, parent=None) -> None:
        super().__init__(parent)
        self.active = active
        self.rows: list[tuple[ManagedLaunch | LaunchHistoryItem, ScriptRecord | None, str]] = []

    @property
    def headers(self) -> Sequence[str]:
        return self.ACTIVE_HEADERS if self.active else self.HISTORY_HEADERS

    def set_launches(
        self,
        launches: dict[str, ManagedLaunch],
        records: dict[str, ScriptRecord],
        display_name,
    ) -> None:
        self.beginResetModel()
        by_path = {str(record.parsed.path): record for record in records.values()}
        self.rows = [
            (launch, by_path.get(launch.script_path),
             display_name(by_path[launch.script_path]) if launch.script_path in by_path
             else Path(launch.script_path).stem)
            for launch in launches.values()
        ]
        self.endResetModel()

    def set_history(
        self,
        entries: list[LaunchHistoryItem],
        records: dict[str, ScriptRecord],
        display_name,
    ) -> None:
        self.beginResetModel()
        by_path = {str(record.parsed.path): record for record in records.values()}
        self.rows = [
            (
                entry,
                by_path.get(entry.script_path),
                display_name(by_path[entry.script_path]) if entry.script_path in by_path
                else Path(entry.script_path).stem,
            )
            for entry in entries
        ]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        launch, record, name = self.rows[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return (
                launch.launch_id if isinstance(launch, ManagedLaunch) else launch.history_id
            )
        if role == Qt.ItemDataRole.ToolTipRole:
            identifier = (
                launch.launch_id if isinstance(launch, ManagedLaunch) else launch.history_id
            )
            return f"{launch.script_path}\nID: {identifier}"
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return (
                Qt.AlignmentFlag.AlignVCenter
                | (
                    Qt.AlignmentFlag.AlignLeft
                    if index.column() in {0, 1}
                    else Qt.AlignmentFlag.AlignHCenter
                )
            )
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        model = (
            record.parsed.model_name if record else
            (launch.model_name if isinstance(launch, LaunchHistoryItem) else "")
        )
        if self.active:
            values = (
                name, model or "—", launch.configured_port or "—", launch.actual_port or "—",
                launch.server_identity.pid if launch.server_identity else "—",
                runtime_display_state(launch.state), api_display_state(launch),
                launch.last_verified_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                if launch.last_verified_at else "—",
            )
        else:
            if isinstance(launch, LaunchHistoryItem):
                values = (
                    name, model or "—", launch.actual_port or launch.configured_port or "—",
                    launch.state, launch.reason or "—",
                    launch.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                )
                return values[index.column()]
            values = (
                name, model or "—", launch.actual_port or launch.configured_port or "—",
                launch.state.value, launch.failure_reason or "—",
                launch.started_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            )
        return values[index.column()]

    def launch_at(self, row: int) -> ManagedLaunch | None:
        if not 0 <= row < len(self.rows):
            return None
        item = self.rows[row][0]
        return item if isinstance(item, ManagedLaunch) else None
