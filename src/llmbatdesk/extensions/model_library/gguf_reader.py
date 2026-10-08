from __future__ import annotations

import json
import math
import os
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .models import GgufMetadata, MetadataValue, Provenance


GGUF_MAGIC = b"GGUF"
SUPPORTED_VERSIONS = {2, 3}
# Increase only when cached parsing semantics or retained fields change.
METADATA_READER_VERSION = 3
METADATA_SCHEMA_VERSION = 2
MAX_METADATA_BYTES = 64 * 1024 * 1024
MAX_KEY_VALUE_COUNT = 100_000
MAX_TENSOR_COUNT = 200_000
MAX_STRING_BYTES = 1024 * 1024
# This is a CPU-work bound, not an allocation bound. Large current tokenizer
# arrays are streamed and may exceed one million elements.
MAX_CONTAINER_ITEMS = 16_000_000
MAX_ARRAY_DEPTH = 4
MAX_TENSOR_DIMENSIONS = 8
MAX_PARAMETER_COUNT = (1 << 63) - 1
_CANCEL_CHECK_INTERVAL = 4096

_FIXED_FORMATS = {
    0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i",
    6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d",
}
_SELECTED_KEYS = {
    "general.name",
    "general.architecture",
    "general.file_type",
    "general.quantization_version",
    "general.size_label",
    "general.basename",
    "general.finetune",
    "general.version",
    "general.type",
    "tokenizer.ggml.model",
}


class GgufFormatError(ValueError):
    pass


class GgufUnsupportedVersion(GgufFormatError):
    pass


class GgufUnsupportedMetadata(GgufFormatError):
    pass


class GgufSafetyLimitError(GgufFormatError):
    pass


class GgufCancelled(GgufFormatError):
    pass


@dataclass(slots=True)
class _BudgetReader:
    handle: BinaryIO
    file_size: int
    limit: int = MAX_METADATA_BYTES
    consumed: int = 0
    cancelled: threading.Event | None = None

    def check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled.is_set():
            raise GgufCancelled("GGUF 元数据读取已取消")

    def _validate_extent(self, size: int) -> None:
        if size < 0 or size > self.limit - self.consumed:
            raise GgufSafetyLimitError("GGUF 元数据超过安全读取上限")
        position = self.handle.tell()
        if position < 0 or size > self.file_size - position:
            raise GgufFormatError("GGUF 文件被截断")

    def read_exact(self, size: int) -> bytes:
        self._validate_extent(size)
        value = self.handle.read(size)
        self.consumed += len(value)
        if len(value) != size:
            raise GgufFormatError("GGUF 文件被截断")
        return value

    def skip_exact(self, size: int) -> None:
        """Advance over validated bytes without allocating them."""
        self._validate_extent(size)
        self.handle.seek(size, os.SEEK_CUR)
        self.consumed += size

    def unpack(self, fmt: str) -> tuple[Any, ...]:
        return struct.unpack(fmt, self.read_exact(struct.calcsize(fmt)))

    def string(self, *, retain: bool = True) -> str | None:
        (length,) = self.unpack("<Q")
        if length > MAX_STRING_BYTES:
            raise GgufSafetyLimitError("GGUF 字符串长度超过安全上限")
        if not retain:
            self.skip_exact(length)
            return None
        raw = self.read_exact(length)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise GgufFormatError("GGUF 元数据包含无效 UTF-8") from error


def _validated_count(count: int) -> None:
    if count > MAX_CONTAINER_ITEMS:
        raise GgufSafetyLimitError("GGUF 容器元素数量超过安全处理上限")


def _skip_array(
    reader: _BudgetReader, element_type: int, count: int, depth: int
) -> None:
    _validated_count(count)
    if element_type in _FIXED_FORMATS:
        width = struct.calcsize(_FIXED_FORMATS[element_type])
        if count > MAX_METADATA_BYTES // width:
            raise GgufSafetyLimitError("GGUF 数组字节长度超过安全上限")
        reader.skip_exact(count * width)
        return
    if element_type == 8:
        for index in range(count):
            if index % _CANCEL_CHECK_INTERVAL == 0:
                reader.check_cancelled()
            reader.string(retain=False)
        return
    if element_type == 9:
        if depth >= MAX_ARRAY_DEPTH:
            raise GgufSafetyLimitError("GGUF 数组嵌套层数超过安全上限")
        for index in range(count):
            if index % _CANCEL_CHECK_INTERVAL == 0:
                reader.check_cancelled()
            nested_type, nested_count = reader.unpack("<IQ")
            _skip_array(reader, nested_type, nested_count, depth + 1)
        return
    raise GgufUnsupportedMetadata(
        f"不支持的 GGUF 元数据类型：{element_type}"
    )


def _value(
    reader: _BudgetReader, value_type: int, *, retain: bool, depth: int = 0
) -> Any:
    if value_type in _FIXED_FORMATS:
        if retain:
            return reader.unpack(_FIXED_FORMATS[value_type])[0]
        reader.skip_exact(struct.calcsize(_FIXED_FORMATS[value_type]))
        return None
    if value_type == 8:
        return reader.string(retain=retain)
    if value_type == 9:
        if depth >= MAX_ARRAY_DEPTH:
            raise GgufSafetyLimitError("GGUF 数组嵌套层数超过安全上限")
        element_type, count = reader.unpack("<IQ")
        # Model-library display fields are scalar. Arrays, including tokenizer
        # tokens/scores/types/merges, are validated and skipped, never retained.
        _skip_array(reader, element_type, count, depth + 1)
        return None
    raise GgufUnsupportedMetadata(
        f"不支持的 GGUF 元数据类型：{value_type}"
    )


def _context_key(architecture: str) -> str:
    return f"{architecture}.context_length" if architecture else ""


def read_gguf_metadata(
    path: Path, *, cancelled: threading.Event | None = None
) -> GgufMetadata:
    """Read bounded metadata/descriptors without touching tensor payloads."""
    with path.open("rb", buffering=0) as handle:
        reader = _BudgetReader(
            handle=handle, file_size=os.fstat(handle.fileno()).st_size,
            cancelled=cancelled,
        )
        if reader.read_exact(4) != GGUF_MAGIC:
            raise GgufFormatError("不是 GGUF 文件")
        version, tensor_count, kv_count = reader.unpack("<IQQ")
        if version not in SUPPORTED_VERSIONS:
            raise GgufUnsupportedVersion(f"不支持的 GGUF 版本：{version}")
        if tensor_count > MAX_TENSOR_COUNT:
            raise GgufSafetyLimitError("GGUF tensor 数量超过安全上限")
        if kv_count > MAX_KEY_VALUE_COUNT:
            raise GgufSafetyLimitError("GGUF 元数据条目数量超过安全上限")

        retained: dict[str, MetadataValue] = {}
        architecture = ""
        deferred: dict[str, Any] = {}
        limited = False
        for index in range(kv_count):
            if index % _CANCEL_CHECK_INTERVAL == 0:
                reader.check_cancelled()
            key_value = reader.string()
            assert isinstance(key_value, str)
            key = key_value
            (value_type,) = reader.unpack("<I")
            wanted = key in _SELECTED_KEYS or key.endswith(".context_length")
            value = _value(reader, value_type, retain=wanted)
            if wanted and value is None:
                limited = True
            elif wanted:
                deferred[key] = value
            if key == "general.architecture" and isinstance(value, str):
                architecture = value

        selected_context = _context_key(architecture)
        for key, value in deferred.items():
            if key in _SELECTED_KEYS or key == selected_context:
                retained[key] = MetadataValue(value, Provenance.METADATA)

        parameter_count = 0
        for index in range(tensor_count):
            if index % _CANCEL_CHECK_INTERVAL == 0:
                reader.check_cancelled()
            reader.string(retain=False)
            (dimension_count,) = reader.unpack("<I")
            if dimension_count > MAX_TENSOR_DIMENSIONS:
                raise GgufSafetyLimitError(
                    "GGUF tensor 维度数量超过安全上限"
                )
            dimensions = reader.unpack("<" + ("Q" * dimension_count))
            reader.unpack("<IQ")  # type and payload offset; never followed
            tensor_parameters = 1
            for dimension in dimensions:
                if dimension == 0:
                    tensor_parameters = 0
                    break
                if tensor_parameters > MAX_PARAMETER_COUNT // dimension:
                    raise GgufSafetyLimitError("GGUF 参数数量溢出")
                tensor_parameters *= dimension
            if parameter_count > MAX_PARAMETER_COUNT - tensor_parameters:
                raise GgufSafetyLimitError("GGUF 参数数量溢出")
            parameter_count += tensor_parameters

        return GgufMetadata(
            version=version,
            tensor_count=tensor_count,
            fields=retained,
            parameter_count=parameter_count,
            bytes_read=reader.consumed,
            warning="部分所需元数据为数组，已安全跳过" if limited else "",
        )


def metadata_to_json(metadata: GgufMetadata) -> tuple[str, str]:
    values = {key: item.value for key, item in metadata.fields.items()}
    values["gguf.version"] = metadata.version
    values["gguf.tensor_count"] = metadata.tensor_count
    provenance = {
        key: item.provenance.value for key, item in metadata.fields.items()
    }
    provenance["parameter_count"] = Provenance.CALCULATED.value
    return (
        json.dumps(values, ensure_ascii=False, separators=(",", ":")),
        json.dumps(provenance, ensure_ascii=False, separators=(",", ":")),
    )


def parameter_scale(value: int | None) -> str:
    if value is None:
        return "未知"
    if value >= 1_000_000_000:
        amount = value / 1_000_000_000
        return (
            f"{amount:.1f}B"
            if not math.isclose(amount, round(amount))
            else f"{round(amount)}B"
        )
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    return str(value)
