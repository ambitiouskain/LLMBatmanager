from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


class Backend(StrEnum):
    LLAMA_CPP = "llama.cpp"
    OLLAMA = "Ollama"
    GENERIC = "通用脚本"


class ParseConfidence(StrEnum):
    FULL = "完全解析"
    PARTIAL = "部分解析"
    UNKNOWN = "未知或动态"


class RunnabilityStatus(StrEnum):
    RUNNABLE = "可运行"
    EXECUTABLE_MISSING = "可执行文件不存在"
    MODEL_MISSING = "模型文件不存在"
    EXECUTABLE_AND_MODEL_MISSING = "可执行文件和模型均不存在"
    DYNAMIC_PATH = "路径包含无法静态解析的内容"
    UNKNOWN_BACKEND = "未知后端"
    NOT_VALIDATED = "未验证"


class RuntimeState(StrEnum):
    NOT_RUNNING = "未运行"
    STARTING = "正在启动"
    PROCESS_RUNNING_PORT_CLOSED = "进程运行中，端口未监听"
    PORT_LISTENING_API_NOT_READY = "端口监听中，API 未就绪"
    API_READY = "API 已就绪"
    RUNNING_NOT_READY = "端口监听中，API 未就绪"
    LOADING = "进程运行中，端口未监听"
    FAILED = "失败"
    STOPPING = "正在停止"
    STOPPED = "已停止"
    DETACHED_UNVERIFIED = "进程已分离或无法验证"
    LOST = "进程已分离或无法验证"
    STALE_RECORD = "过期运行记录"
    AUTH_REQUIRED = "API 需要认证"
    LEGACY_LOADING = "正在加载模型"
    LEGACY_RUNNING_NOT_READY = "进程运行中，API 未就绪"
    LEGACY_LOST = "进程丢失或已分离"


class ApiReadinessStatus(StrEnum):
    NOT_CHECKED = "尚未检查"
    PENDING = "检查中"
    READY = "API 已就绪"
    AUTH_REQUIRED = "需要认证"
    INCOMPATIBLE = "端点不兼容"
    TIMED_OUT = "请求超时"
    PROCESS_EXITED = "进程已退出"
    ERROR = "检查失败"


class LaunchMode(StrEnum):
    BACKGROUND = "background"
    VISIBLE = "visible"


class LaunchModeOverride(StrEnum):
    GLOBAL = "global"
    BACKGROUND = "background"
    VISIBLE = "visible"


class LaunchConfirmationMode(StrEnum):
    ALWAYS = "always"
    FIRST_OR_CHANGED = "first_or_changed"
    TRUSTED_SKIP = "trusted_skip"


class EditorMode(StrEnum):
    SYSTEM_DEFAULT = "system_default"
    CUSTOM = "custom"


class ScriptRemovalMode(StrEnum):
    LIBRARY_ONLY = "library_only"
    RECYCLE_BIN = "recycle_bin"


class IgnoredScriptRecord(BaseModel):
    canonical_path: str
    root_path: str
    ignored_at: datetime = Field(default_factory=datetime.now)


class ScriptDiscoveryInfo(BaseModel):
    canonical_path: str
    owning_root: str | None = None
    individually_added: bool = False

    @property
    def discovered_from_root(self) -> bool:
        return self.owning_root is not None

    @property
    def source_label(self) -> str:
        if self.discovered_from_root and self.individually_added:
            return "扫描目录及单独添加"
        if self.discovered_from_root:
            return "配置的扫描目录"
        if self.individually_added:
            return "单独添加"
        return "未知来源"


class ScriptRemovalResult(BaseModel):
    canonical_path: str
    mode: ScriptRemovalMode
    ignored: bool = False
    recycled: bool = False
    message: str


ACTIVE_RUNTIME_STATES = {
    RuntimeState.PROCESS_RUNNING_PORT_CLOSED,
    RuntimeState.PORT_LISTENING_API_NOT_READY,
    RuntimeState.API_READY,
    RuntimeState.AUTH_REQUIRED,
    RuntimeState.STOPPING,
}


class RunnabilityReport(BaseModel):
    status: RunnabilityStatus = RunnabilityStatus.NOT_VALIDATED
    launch_allowed: bool = True
    resolved_executable: str | None = None
    executable_exists: bool | None = None
    model_exists: bool | None = None
    warnings: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    line_number: int
    source_line: str
    normalized_token: str
    field: str
    reason: str


class PortSource(BaseModel):
    line_number: int
    start: int
    end: int
    original: str
    style: str
    safe: bool = True
    reason: str = ""


class ScriptFingerprint(BaseModel):
    canonical_path: str
    size: int
    mtime_ns: int
    sha256: str


class ParsedScript(BaseModel):
    path: Path
    encoding: str = "utf-8"
    newline: str = "\r\n"
    backend: Backend = Backend.GENERIC
    confidence: ParseConfidence = ParseConfidence.UNKNOWN
    executable: str | None = None
    model_name: str | None = None
    model_path: str | None = None
    model_exists: bool | None = None
    bind_host: str | None = None
    configured_port: int | None = None
    context_size: int | None = None
    gpu_layers: int | Literal["auto", "all"] | None = None
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    environment: dict[str, str] = Field(default_factory=dict)
    other_arguments: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dynamic_reasons: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    port_source: PortSource | None = None
    raw_text: str = ""
    content_hash: str = ""
    modified_at: datetime | None = None
    has_api_key: bool = False
    uses_start: bool = False
    uses_call: bool = False
    interactive_commands: list[str] = Field(default_factory=list)
    harmless_trailing_pause: bool = False
    bind_address: str | None = None
    client_address: str | None = None

    @property
    def api_address(self) -> str | None:
        return self.client_address


class ScriptRecord(BaseModel):
    fingerprint: ScriptFingerprint
    parsed: ParsedScript
    runnability: RunnabilityReport = Field(default_factory=RunnabilityReport)


class Metadata(BaseModel):
    canonical_path: str
    display_name: str = ""
    favorite: bool = False
    tags: list[str] = Field(default_factory=list)
    games: list[str] = Field(default_factory=list)
    category: str = ""
    purpose: str = ""
    strengths: str = ""
    weaknesses: str = ""
    notes: str = ""
    sort_order: int = 0
    last_run_at: datetime | None = None
    launch_mode_override: LaunchModeOverride = LaunchModeOverride.GLOBAL


class ProcessIdentity(BaseModel):
    pid: int
    create_time: float
    executable: str | None = None
    command_line: list[str] = Field(default_factory=list)
    port: int | None = None


class ManagedLaunch(BaseModel):
    launch_id: str
    script_path: str
    script_hash: str
    identity: ProcessIdentity
    child_identities: list[ProcessIdentity] = Field(default_factory=list)
    server_identity: ProcessIdentity | None = None
    configured_port: int | None = None
    actual_port: int | None = None
    log_path: str
    temporary_script_path: str | None = None
    state: RuntimeState = RuntimeState.STARTING
    started_at: datetime = Field(default_factory=datetime.now)
    last_verified_at: datetime | None = None
    api_ready: bool = False
    api_status: ApiReadinessStatus = ApiReadinessStatus.NOT_CHECKED
    api_checked_url: str = ""
    api_last_http_status: int | None = None
    api_last_error: str = ""
    api_last_check_at: datetime | None = None
    api_retry_count: int = 0
    api_check_generation: int = 0
    verified: bool = False
    failure_reason: str = ""
    user_stopped: bool = False


class ApiCheckResult(BaseModel):
    ready: bool
    state: RuntimeState
    status_code: int | None = None
    detail: str = ""
    checked_url: str = ""
    error_kind: str = ""
    retry_count: int = 0
    loaded_models: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=datetime.now)


class PortOccupant(BaseModel):
    port: int
    pid: int | None = None
    process_name: str | None = None
    executable: str | None = None
    managed_launch_id: str | None = None


class OperationEvent(BaseModel):
    kind: str
    timestamp: datetime = Field(default_factory=datetime.now)
    data: dict[str, Any] = Field(default_factory=dict)


class LaunchHistoryItem(BaseModel):
    history_id: str
    script_path: str
    model_name: str = ""
    configured_port: int | None = None
    actual_port: int | None = None
    state: str
    reason: str = ""
    timestamp: datetime = Field(default_factory=datetime.now)
