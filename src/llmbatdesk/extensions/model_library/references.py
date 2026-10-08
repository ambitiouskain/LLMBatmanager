from __future__ import annotations

import re
from pathlib import Path

from ...domain.models import Backend, ParseConfidence, ScriptRecord
from .models import ReferenceRole, ReferenceState, ScriptReference


_FIELD_ROLES = {
    "model_path": ReferenceRole.PRIMARY,
    "mmproj_path": ReferenceRole.MMPROJ,
    "draft_model_path": ReferenceRole.DRAFT,
    "adapter_path": ReferenceRole.ADAPTER,
    "control_path": ReferenceRole.CONTROL,
}
_DYNAMIC_MARKERS = re.compile(r"%[^%]+%|![^!]+!")


def _canonical_model_path(record: ScriptRecord, value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = record.parsed.path.parent / path
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _evidence_value(token: str) -> str:
    """Extract a value from already-tokenized parser evidence."""
    value = str(token).strip()
    if "=" in value:
        return value.split("=", 1)[1].strip().strip('"')
    parts = value.split(maxsplit=1)
    return parts[1].strip().strip('"') if len(parts) == 2 else value.strip('"')


def _relationship(
    record: ScriptRecord, role: ReferenceRole, values: set[str]
) -> list[ScriptReference]:
    parsed = record.parsed
    script_path = str(parsed.path)
    script_name = parsed.path.stem
    dynamic = {
        value for value in values if _DYNAMIC_MARKERS.search(value)
    }
    concrete = {
        _canonical_model_path(record, value)
        for value in values - dynamic
        if value.casefold().endswith(".gguf")
    }
    if dynamic:
        return [ScriptReference(
            script_path=script_path, script_name=script_name,
            state=ReferenceState.DYNAMIC, role=ReferenceRole.DYNAMIC,
            reason="模型路径包含未解析的动态变量",
            candidates=sorted(dynamic, key=str.casefold),
        )]
    if len(concrete) > 1:
        return [ScriptReference(
            script_path=script_path, script_name=script_name,
            state=ReferenceState.MULTIPLE, role=ReferenceRole.AMBIGUOUS,
            reason=(
                "检测到多个不同的主模型参数"
                if role == ReferenceRole.PRIMARY
                else "检测到多个不同的辅助模型参数"
            ),
            candidates=sorted(concrete, key=str.casefold),
        )]
    if len(concrete) == 1:
        model_path = next(iter(concrete))
        exists = Path(model_path).is_file()
        return [ScriptReference(
            script_path=script_path, script_name=script_name, role=role,
            state=ReferenceState.EXPLICIT if exists else ReferenceState.MISSING,
            model_path=model_path,
            reason="" if exists else "明确引用的文件当前不存在",
        )]
    return []


def references_from_records(records: list[ScriptRecord]) -> list[ScriptReference]:
    """Build role-aware relationships solely from audited parser output."""
    relationships: list[ScriptReference] = []
    for record in records:
        parsed = record.parsed
        values: dict[ReferenceRole, set[str]] = {
            role: set() for role in _FIELD_ROLES.values()
        }
        for item in parsed.evidence:
            role = _FIELD_ROLES.get(item.field)
            if role is not None:
                values[role].add(_evidence_value(item.normalized_token))
        if parsed.model_path:
            values[ReferenceRole.PRIMARY].add(parsed.model_path)

        # Partial/dynamic parser output cannot safely establish which command
        # actually runs. Keep paths as diagnostic candidates, not model links.
        unresolved_behavior = bool(parsed.dynamic_reasons) and (
            parsed.confidence in {
                ParseConfidence.PARTIAL, ParseConfidence.UNKNOWN
            }
        )
        if unresolved_behavior:
            candidates = sorted(
                {value for group in values.values() for value in group},
                key=str.casefold,
            )
            relationships.append(ScriptReference(
                script_path=str(parsed.path), script_name=parsed.path.stem,
                state=ReferenceState.DYNAMIC, role=ReferenceRole.DYNAMIC,
                candidates=candidates,
                reason="脚本包含动态、分支或包装行为，无法确认实际模型引用",
            ))
            continue

        per_script: list[ScriptReference] = []
        for role, candidates in values.items():
            per_script.extend(_relationship(record, role, candidates))
        if per_script:
            relationships.extend(per_script)
            continue

        if parsed.confidence in {
            ParseConfidence.PARTIAL, ParseConfidence.UNKNOWN
        }:
            state = ReferenceState.UNPARSED
            reason = "现有脚本解析结果不足以确认模型路径"
        else:
            state = ReferenceState.NO_LOCAL_GGUF
            reason = (
                "该脚本不使用明确的本地 GGUF 路径"
                if parsed.backend == Backend.OLLAMA
                else "未检测到本地 GGUF 引用"
            )
        relationships.append(ScriptReference(
            script_path=str(parsed.path), script_name=parsed.path.stem,
            state=state, role=ReferenceRole.PRIMARY, reason=reason,
        ))
    return relationships
