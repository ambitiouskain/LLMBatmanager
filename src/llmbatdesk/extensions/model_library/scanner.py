from __future__ import annotations

import os
import json
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

from .gguf_reader import (
    GgufCancelled,
    GgufFormatError,
    GgufSafetyLimitError,
    GgufUnsupportedMetadata,
    GgufUnsupportedVersion,
    METADATA_READER_VERSION,
    METADATA_SCHEMA_VERSION,
    metadata_to_json,
    read_gguf_metadata,
)
from .models import (
    ModelRecord, ModelStatus, ParseStatus, ScanRoot, ScanSummary,
)
from .storage import ModelLibraryStore
from .quantization import quantization_label
from .presentation import choose_model_size, role_from_metadata
from .gguf_reader import parameter_scale


FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_QUANTIZATION_FILENAME = re.compile(
    r"(?:^|[._-])("
    r"IQ\d(?:_[A-Z0-9]+)*|Q\d(?:_[A-Z0-9]+)*|"
    r"TQ[12]_0|MXFP4(?:_MOE)?|NVFP4|F16|F32|BF16"
    r")(?:[._-]|$)",
    re.IGNORECASE,
)


def is_reparse_point(path: Path, entry_stat: os.stat_result | None = None) -> bool:
    try:
        value = entry_stat or path.lstat()
    except OSError:
        return True
    return bool(
        stat.S_ISLNK(value.st_mode)
        or getattr(value, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def canonical_scan_root(path: Path) -> Path:
    if not path.exists() or not path.is_dir():
        raise ValueError(f"扫描目录不存在或不可访问：{path}")
    if is_reparse_point(path):
        raise ValueError("扫描根目录不能是符号链接、联接点或重解析点")
    canonical = path.resolve(strict=True)
    if canonical == Path(canonical.anchor):
        raise ValueError("不能将整个驱动器或文件系统根目录作为扫描目录")
    return canonical


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def iter_gguf_files(
    root: Path, *, recursive: bool, cancelled: threading.Event,
    error_callback: Callable[[Path, OSError], None] | None = None,
) -> Iterator[tuple[Path, os.stat_result]]:
    stack = [root]
    while stack and not cancelled.is_set():
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if cancelled.is_set():
                        return
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    path = Path(entry.path)
                    if is_reparse_point(path, entry_stat):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if recursive:
                            try:
                                resolved = path.resolve(strict=True)
                            except OSError as error:
                                if error_callback:
                                    error_callback(path, error)
                                continue
                            if _inside_root(resolved, root):
                                stack.append(resolved)
                        continue
                    if (
                        entry.is_file(follow_symlinks=False)
                        and entry.name.casefold().endswith(".gguf")
                    ):
                        try:
                            resolved = path.resolve(strict=True)
                        except OSError as error:
                            if error_callback:
                                error_callback(path, error)
                            continue
                        if _inside_root(resolved, root):
                            yield resolved, entry_stat
        except (OSError, PermissionError) as error:
            if error_callback:
                error_callback(directory, error)
            continue


def _field(metadata, key: str, default=""):
    value = metadata.value(key)
    return value if value is not None else default


def _record_from_metadata(
    root_id: int, path: Path, file_stat: os.stat_result, scanned_at: str,
    cancelled: threading.Event | None = None,
) -> ModelRecord:
    try:
        metadata = read_gguf_metadata(path, cancelled=cancelled)
    except TypeError as error:
        # Backward-compatible seam for injected/test metadata readers that
        # predate cooperative cancellation.
        if "cancelled" not in str(error):
            raise
        metadata = read_gguf_metadata(path)
    after = path.stat()
    if (
        after.st_size != file_stat.st_size
        or after.st_mtime_ns != file_stat.st_mtime_ns
    ):
        raise GgufFormatError("读取期间文件发生变化，请重新扫描")
    metadata_json, provenance_json = metadata_to_json(metadata)
    has_name = metadata.value("general.name") is not None
    name = str(_field(metadata, "general.name", path.stem))
    architecture = str(_field(metadata, "general.architecture", ""))
    context_key = f"{architecture}.context_length" if architecture else ""
    context_value = metadata.value(context_key) if context_key else None
    file_type = _field(metadata, "general.file_type", None)
    quantization = quantization_label(file_type)
    if not quantization:
        match = _QUANTIZATION_FILENAME.search(path.stem)
        if match:
            quantization = match.group(1).upper()
    provenance = json.loads(provenance_json)
    if quantization and file_type is None:
        provenance["quantization"] = "filename heuristic"
    general_type = str(_field(metadata, "general.type", ""))
    metadata_role, role_provenance = role_from_metadata(
        general_type=general_type, architecture=architecture,
        filename=path.name, tensor_count=metadata.tensor_count,
    )
    nominal_size, size_provenance = choose_model_size(
        metadata_size_label=str(_field(metadata, "general.size_label", "")),
        model_name=name, filename=path.name,
        parameter_count=metadata.parameter_count,
        calculated_formatter=parameter_scale,
    )
    auxiliary = bool(
        general_type and general_type.casefold() not in {"model", "unknown"}
    ) or (not architecture and metadata.tensor_count == 0)
    unknown_reason = (
        "辅助或最小 GGUF 未提供此字段"
        if auxiliary else "文件未提供受支持的元数据字段"
    )
    provenance.update(
        {
            "display.name": "metadata" if has_name else "filename heuristic",
            "display.architecture": (
                "metadata" if architecture else f"unknown: {unknown_reason}"
            ),
            "display.parameter_count": "calculated",
            "display.quantization": (
                "metadata" if file_type is not None
                else "filename heuristic" if quantization else "unknown"
            ),
            "display.context_length": (
                "metadata" if context_value is not None
                else f"unknown: {unknown_reason}"
            ),
            "display.model_kind": (
                f"metadata: {general_type}" if general_type
                else "calculated: auxiliary/minimal" if auxiliary
                else "unknown: 未提供 general.type"
            ),
            "display.nominal_size": size_provenance,
            "display.role": role_provenance,
        }
    )
    provenance_json = json.dumps(
        provenance, ensure_ascii=False, separators=(",", ":")
    )
    return ModelRecord(
        model_id=None, root_id=root_id, canonical_path=str(path),
        filename=path.name, size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns, status=ModelStatus.AVAILABLE,
        parse_status=(
            ParseStatus.LIMITED if metadata.warning else ParseStatus.PARSED
        ),
        parse_error=metadata.warning, name=name,
        metadata_reader_version=METADATA_READER_VERSION,
        metadata_schema_version=METADATA_SCHEMA_VERSION,
        last_parse_outcome_class=(
            "limited" if metadata.warning else "success"
        ),
        architecture=architecture, parameter_count=metadata.parameter_count,
        nominal_size=nominal_size, size_provenance=size_provenance,
        quantization=quantization,
        file_type=file_type if isinstance(file_type, int) else None,
        size_label=str(_field(metadata, "general.size_label", "")),
        tokenizer_family=str(_field(metadata, "tokenizer.ggml.model", "")),
        context_length=(
            int(context_value)
            if isinstance(context_value, int) and context_value >= 0 else None
        ),
        tensor_count=metadata.tensor_count, metadata_json=metadata_json,
        provenance_json=provenance_json,
        model_role=metadata_role, role_provenance=role_provenance,
        metadata_role=metadata_role,
        metadata_role_provenance=role_provenance,
        last_scanned_at=scanned_at,
    )


class ModelScanner:
    def __init__(
        self, store: ModelLibraryStore,
        reader: Callable[[Path], object] | None = None,
    ) -> None:
        self.store = store
        # Dependency injection is retained for deterministic no-file-open tests.
        self.reader = reader

    def scan(
        self,
        job_id: str,
        roots: list[ScanRoot],
        cancelled: threading.Event,
        *,
        batch_callback: Callable[[str, list[str]], None] | None = None,
    ) -> ScanSummary:
        summary = ScanSummary(
            job_id=job_id,
            root_ids=[root.root_id for root in roots if root.root_id is not None],
        )
        batch: list[str] = []
        for configured in roots:
            if cancelled.is_set():
                summary.cancelled = True
                break
            if configured.root_id is None or not configured.enabled:
                continue
            scan_time = datetime.now().astimezone().isoformat()
            try:
                root = canonical_scan_root(Path(configured.canonical_path))
            except (OSError, ValueError) as error:
                message = str(error)
                summary.messages.append(message)
                self.store.complete_root_scan(
                    configured.root_id, available=False,
                    message=message, scanned_at=None,
                )
                continue
            seen: set[str] = set()
            directory_errors: list[str] = []
            try:
                with self.store.scan_session() as scan_connection:
                    iterator = iter_gguf_files(
                        root, recursive=configured.recursive, cancelled=cancelled,
                        error_callback=lambda path, error: directory_errors.append(
                            f"{path}: {error}"
                        ),
                    )
                    pending_writes = 0
                    for path, file_stat in iterator:
                        if cancelled.is_set():
                            summary.cancelled = True
                            break
                        canonical = str(path)
                        seen.add(canonical)
                        summary.discovered += 1
                        cached = self.store.cached_parse_state(
                            canonical, scan_connection
                        )
                        identity_matches = bool(
                            cached
                            and cached[:2] == (
                                file_stat.st_size, file_stat.st_mtime_ns
                            )
                        )
                        current_cache = bool(
                            identity_matches
                            and cached[2] >= METADATA_READER_VERSION
                            and cached[3] >= METADATA_SCHEMA_VERSION
                        )
                        transient = bool(
                            cached
                            and cached[4] in {
                                "io_error", "interrupted", "cancelled",
                                "file_changed"
                            }
                        )
                        if current_cache and not transient:
                            summary.unchanged += 1
                        else:
                            if identity_matches and not current_cache:
                                notice = (
                                    "旧版元数据缓存，将在本次手动扫描中重新读取"
                                )
                                if notice not in summary.messages:
                                    summary.messages.append(notice)
                            try:
                                record = (
                                    self.reader(configured.root_id, path, file_stat, scan_time)
                                    if self.reader is not None
                                    else _record_from_metadata(
                                        configured.root_id, path, file_stat,
                                        scan_time, cancelled,
                                    )
                                )
                            except GgufCancelled:
                                summary.cancelled = True
                                break
                            except (
                                OSError, ValueError, GgufFormatError
                            ) as error:
                                if isinstance(error, GgufUnsupportedVersion):
                                    parse_status = ParseStatus.UNSUPPORTED_VERSION
                                    outcome_class = "unsupported_version"
                                elif isinstance(error, GgufUnsupportedMetadata):
                                    parse_status = ParseStatus.UNSUPPORTED_METADATA
                                    outcome_class = "unsupported_metadata"
                                elif isinstance(error, GgufSafetyLimitError):
                                    parse_status = ParseStatus.SAFETY_LIMIT
                                    outcome_class = "safety_limit"
                                elif isinstance(error, OSError):
                                    parse_status = ParseStatus.IO_ERROR
                                    outcome_class = "io_error"
                                else:
                                    parse_status = ParseStatus.MALFORMED
                                    outcome_class = (
                                        "file_changed"
                                        if "读取期间文件发生变化" in str(error)
                                        else "malformed"
                                    )
                                record = ModelRecord(
                                    model_id=None, root_id=configured.root_id,
                                    canonical_path=canonical, filename=path.name,
                                    size=file_stat.st_size,
                                    mtime_ns=file_stat.st_mtime_ns,
                                    status=ModelStatus.ERROR,
                                    parse_status=parse_status,
                                    parse_error=str(error),
                                    metadata_reader_version=(
                                        METADATA_READER_VERSION
                                    ),
                                    metadata_schema_version=(
                                        METADATA_SCHEMA_VERSION
                                    ),
                                    last_parse_outcome_class=outcome_class,
                                    last_scanned_at=scan_time,
                                )
                                summary.failed += 1
                            else:
                                record.metadata_reader_version = (
                                    METADATA_READER_VERSION
                                )
                                record.metadata_schema_version = (
                                    METADATA_SCHEMA_VERSION
                                )
                                if record.last_parse_outcome_class == "legacy":
                                    record.last_parse_outcome_class = "success"
                                summary.parsed += 1
                            self.store.upsert_model(record, scan_connection)
                            pending_writes += 1
                        batch.append(canonical)
                        if len(batch) >= 50:
                            scan_connection.commit()
                            pending_writes = 0
                            if batch_callback:
                                batch_callback(job_id, batch)
                            batch = []
                        # Cooperative yield without maintaining a polling loop.
                        time.sleep(0)
                    if pending_writes:
                        scan_connection.commit()
            except (OSError, PermissionError) as error:
                summary.messages.append(f"{root}: {error}")
            if cancelled.is_set():
                summary.cancelled = True
                break
            summary.missing += self.store.mark_missing_after_scan(
                configured.root_id, seen, scan_time
            )
            if directory_errors:
                summary.messages.extend(directory_errors[:20])
            self.store.complete_root_scan(
                configured.root_id,
                available=not any(
                    message.startswith(f"{root}:")
                    for message in directory_errors
                ),
                message="\n".join(directory_errors[:20]),
                scanned_at=scan_time,
            )
        if batch and batch_callback:
            batch_callback(job_id, batch)
        if cancelled.is_set():
            summary.cancelled = True
        return summary
