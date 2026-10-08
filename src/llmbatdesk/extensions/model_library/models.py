from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ModelStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    ERROR = "error"


class ParseStatus(StrEnum):
    PARSED = "parsed"
    UNSUPPORTED_METADATA = "unsupported_metadata"
    LIMITED = "limited"
    UNSUPPORTED_VERSION = "unsupported_version"
    MALFORMED = "malformed"
    SAFETY_LIMIT = "safety_limit"
    IO_ERROR = "io_error"
    ERROR = "error"  # legacy persisted value
    UNKNOWN = "unknown"


class Provenance(StrEnum):
    METADATA = "metadata"
    CALCULATED = "calculated"
    FILENAME = "filename"
    UNKNOWN = "unknown"


class ReferenceState(StrEnum):
    EXPLICIT = "explicit"
    MISSING = "missing"
    MULTIPLE = "multiple"
    DYNAMIC = "dynamic"
    NO_LOCAL_GGUF = "no_local_gguf"
    UNPARSED = "unparsed"
    AMBIGUOUS = "ambiguous"


class ReferenceRole(StrEnum):
    PRIMARY = "primary"
    MMPROJ = "mmproj"
    DRAFT = "draft"
    ADAPTER = "adapter"
    CONTROL = "control"
    AUXILIARY = "auxiliary"
    DYNAMIC = "dynamic"
    AMBIGUOUS = "ambiguous"


class ModelFileRole(StrEnum):
    PRIMARY = "primary"
    MMPROJ = "mmproj"
    DRAFT = "draft"
    ADAPTER = "adapter"
    CONTROL = "control"
    EMBEDDING = "embedding"
    AUXILIARY = "auxiliary"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ScanRoot:
    root_id: int | None
    canonical_path: str
    display_name: str
    enabled: bool = True
    recursive: bool = True
    last_scan_at: str | None = None
    indexed_count: int = 0
    available: bool = True
    status_message: str = ""


@dataclass(slots=True)
class MetadataValue:
    value: Any = None
    provenance: Provenance = Provenance.UNKNOWN


@dataclass(slots=True)
class GgufMetadata:
    version: int
    tensor_count: int
    fields: dict[str, MetadataValue] = field(default_factory=dict)
    parameter_count: int | None = None
    bytes_read: int = 0
    warning: str = ""

    def value(self, key: str) -> Any:
        item = self.fields.get(key)
        return item.value if item else None


@dataclass(slots=True)
class ModelRecord:
    model_id: int | None
    root_id: int
    canonical_path: str
    filename: str
    size: int
    mtime_ns: int
    status: ModelStatus = ModelStatus.AVAILABLE
    parse_status: ParseStatus = ParseStatus.UNKNOWN
    parse_error: str = ""
    metadata_reader_version: int = 0
    metadata_schema_version: int = 0
    last_parse_outcome_class: str = "legacy"
    name: str = ""
    architecture: str = ""
    architecture_family: str = ""
    architecture_provenance: str = "unknown"
    parameter_count: int | None = None
    active_parameter_count: int | None = None
    nominal_size: str = ""
    size_provenance: str = "unknown"
    quantization: str = ""
    file_type: int | None = None
    size_label: str = ""
    tokenizer_family: str = ""
    context_length: int | None = None
    tensor_count: int | None = None
    metadata_json: str = "{}"
    provenance_json: str = "{}"
    tags: list[str] = field(default_factory=list)
    notes: str = ""
    reference_count: int = 0
    primary_reference_count: int = 0
    auxiliary_reference_count: int = 0
    related_scripts: list[str] = field(default_factory=list)
    possible_duplicate: bool = False
    model_role: ModelFileRole = ModelFileRole.UNKNOWN
    role_provenance: str = "unknown"
    metadata_role: ModelFileRole = ModelFileRole.UNKNOWN
    metadata_role_provenance: str = "unknown"
    last_scanned_at: str | None = None

    @property
    def path(self) -> Path:
        return Path(self.canonical_path)


@dataclass(slots=True)
class ScriptReference:
    script_path: str
    script_name: str
    state: ReferenceState
    model_path: str = ""
    candidates: list[str] = field(default_factory=list)
    reason: str = ""
    role: ReferenceRole = ReferenceRole.PRIMARY


@dataclass(slots=True)
class AssociatedComponent:
    canonical_path: str
    filename: str
    role: ModelFileRole
    script_path: str
    script_name: str
    existence_state: str
    confidence: str
    model_id: int | None = None


@dataclass(slots=True)
class ScanSummary:
    job_id: str
    root_ids: list[int]
    discovered: int = 0
    parsed: int = 0
    unchanged: int = 0
    failed: int = 0
    missing: int = 0
    cancelled: bool = False
    messages: list[str] = field(default_factory=list)


class ModelLibraryError(RuntimeError):
    pass


class ScanSuppressedError(ModelLibraryError):
    pass
