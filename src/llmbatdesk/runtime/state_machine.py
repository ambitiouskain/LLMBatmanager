from __future__ import annotations

from ..domain.models import RuntimeState


VALID_TRANSITIONS: dict[RuntimeState, set[RuntimeState]] = {
    RuntimeState.NOT_RUNNING: {RuntimeState.STARTING},
    RuntimeState.STARTING: {
        RuntimeState.PROCESS_RUNNING_PORT_CLOSED,
        RuntimeState.PORT_LISTENING_API_NOT_READY,
        RuntimeState.API_READY,
        RuntimeState.AUTH_REQUIRED,
        RuntimeState.FAILED,
        RuntimeState.DETACHED_UNVERIFIED,
        RuntimeState.STALE_RECORD,
        RuntimeState.STOPPING,
    },
    RuntimeState.PROCESS_RUNNING_PORT_CLOSED: {
        RuntimeState.PORT_LISTENING_API_NOT_READY,
        RuntimeState.API_READY,
        RuntimeState.AUTH_REQUIRED,
        RuntimeState.FAILED,
        RuntimeState.DETACHED_UNVERIFIED,
        RuntimeState.STOPPING,
    },
    RuntimeState.PORT_LISTENING_API_NOT_READY: {
        RuntimeState.API_READY,
        RuntimeState.AUTH_REQUIRED,
        RuntimeState.PROCESS_RUNNING_PORT_CLOSED,
        RuntimeState.FAILED,
        RuntimeState.DETACHED_UNVERIFIED,
        RuntimeState.STOPPING,
    },
    RuntimeState.API_READY: {
        RuntimeState.PORT_LISTENING_API_NOT_READY,
        RuntimeState.FAILED,
        RuntimeState.DETACHED_UNVERIFIED,
        RuntimeState.STOPPING,
    },
    RuntimeState.AUTH_REQUIRED: {
        RuntimeState.API_READY,
        RuntimeState.PORT_LISTENING_API_NOT_READY,
        RuntimeState.FAILED,
        RuntimeState.DETACHED_UNVERIFIED,
        RuntimeState.STOPPING,
    },
    RuntimeState.STOPPING: {
        RuntimeState.STOPPED, RuntimeState.FAILED, RuntimeState.DETACHED_UNVERIFIED,
    },
    RuntimeState.DETACHED_UNVERIFIED: {
        RuntimeState.STALE_RECORD, RuntimeState.FAILED, RuntimeState.STOPPED,
    },
    RuntimeState.STALE_RECORD: set(),
    RuntimeState.STOPPED: set(),
    RuntimeState.FAILED: set(),
    RuntimeState.LEGACY_LOADING: {RuntimeState.STALE_RECORD, RuntimeState.FAILED},
    RuntimeState.LEGACY_RUNNING_NOT_READY: {RuntimeState.STALE_RECORD, RuntimeState.FAILED},
    RuntimeState.LEGACY_LOST: {RuntimeState.STALE_RECORD},
}


def transition(current: RuntimeState, target: RuntimeState) -> RuntimeState:
    if current == target:
        return current
    if target not in VALID_TRANSITIONS.get(current, set()):
        raise ValueError(f"无效运行状态转换：{current.name} -> {target.name}")
    return target
