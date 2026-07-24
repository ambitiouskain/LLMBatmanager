from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..domain.models import ParsedScript
from ..parsing import parse_script_bytes
from ..parsing.tokenizer import decode_batch


class OverrideRefused(ValueError):
    pass


@dataclass(frozen=True)
class OverrideResult:
    path: Path
    data: bytes
    diff: str
    original_port: int
    new_port: int


def generate_port_override(
    parsed: ParsedScript, original_data: bytes, new_port: int, output_dir: Path
) -> OverrideResult:
    if not 1 <= new_port <= 65535:
        raise OverrideRefused("端口必须在 1 到 65535 之间")
    if parsed.configured_port is None or parsed.port_source is None:
        reason = parsed.dynamic_reasons[0] if parsed.dynamic_reasons else "没有唯一且静态的端口来源"
        raise OverrideRefused(f"无法安全覆盖端口：{reason}")
    if parsed.dynamic_reasons:
        raise OverrideRefused(f"无法安全覆盖动态脚本：{parsed.dynamic_reasons[0]}")
    if parsed.content_hash != parse_script_bytes(original_data, parsed.path).content_hash:
        raise OverrideRefused("原脚本内容已变化，请重新扫描")
    text, encoding = decode_batch(original_data)
    if text.encode(encoding) != original_data:
        raise OverrideRefused("无法无损保留此脚本编码")
    source = parsed.port_source
    if text[source.start:source.end] != source.original:
        raise OverrideRefused("已验证的端口位置与当前文件不一致")
    updated = text[:source.start] + str(new_port) + text[source.end:]
    new_data = updated.encode(encoding)
    before_lines, after_lines = text.splitlines(keepends=True), updated.splitlines(keepends=True)
    changed = [
        index for index, (before, after) in enumerate(zip(before_lines, after_lines), 1)
        if before != after
    ]
    if changed != [source.line_number] or len(before_lines) != len(after_lines):
        raise OverrideRefused("安全校验失败：端口以外的行发生变化")
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{parsed.path.stem}-{new_port}-{uuid4().hex[:8]}{parsed.path.suffix}"
    destination.write_bytes(new_data)
    if parsed.path.exists() and parsed.path.read_bytes() != original_data:
        destination.unlink(missing_ok=True)
        raise OverrideRefused("创建临时副本期间原脚本发生变化")
    diff = "".join(difflib.unified_diff(
        before_lines, after_lines, fromfile=str(parsed.path), tofile=str(destination)
    ))
    return OverrideResult(destination, new_data, diff, parsed.configured_port, new_port)


def save_managed_copy(
    override: OverrideResult, destination: Path, original_path: Path
) -> Path:
    if destination.exists():
        raise FileExistsError(f"不会覆盖已有文件：{destination}")
    text, encoding = decode_batch(override.data)
    header = (
        f"rem LLMBatDesk generated copy; source: {original_path}\r\n"
        if "\r\n" in text else f"rem LLMBatDesk generated copy; source: {original_path}\n"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((header + text).encode(encoding))
    return destination

