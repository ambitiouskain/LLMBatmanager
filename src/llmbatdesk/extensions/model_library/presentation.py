from __future__ import annotations

import re
from pathlib import Path

from .models import ModelFileRole, ReferenceRole


ARCHITECTURE_NAMES: dict[str, str] = {
    "llama": "Llama 系列",
    "gemma": "Gemma",
    "gemma2": "Gemma 2",
    "gemma3": "Gemma 3",
    "gemma4": "Gemma 4",
    "gemma4-assistant": "Gemma 4 辅助模型",
    "qwen": "Qwen 系列",
    "qwen2": "Qwen 2 系列",
    "qwen2moe": "Qwen 2 MoE 系列",
    "qwen3": "Qwen 3 系列",
    # qwen35 is an implementation identifier, not a safe marketing-version
    # assertion, so keep the deliberately broader family label.
    "qwen35": "Qwen 系列",
    "clip": "视觉编码器",
    "hunyuan-dense": "Hunyuan Dense",
    "bert": "BERT 系列",
    "nomic-bert": "Nomic BERT",
}

_SIZE_LABEL = re.compile(
    r"(?i)(?<![A-Z0-9])"
    r"(\d+(?:\.\d+)?[KMBTQ](?:-A\d+(?:\.\d+)?[KMBTQ])?)"
    r"(?![A-Z0-9])"
)


def friendly_architecture(raw: str) -> tuple[str, str]:
    value = raw.strip().casefold()
    if not value:
        return "未知", "unknown: 未提供 general.architecture"
    if value in ARCHITECTURE_NAMES:
        return ARCHITECTURE_NAMES[value], "mapping"
    return f"未知架构（{raw}）", "fallback"


def _valid_metadata_size_label(value: str) -> str:
    label = value.strip()
    if not label or len(label) > 64:
        return ""
    # GGUF permits expert-style labels as well as simple 7B/26B-A4B labels.
    if re.fullmatch(
        r"(?i)(?:\d+x)?\d+(?:\.\d+)?[KMBTQ]"
        r"(?:-A\d+(?:\.\d+)?[KMBTQ])?",
        label,
    ):
        return label.upper()
    return ""


def _label_from_text(value: str) -> str:
    match = _SIZE_LABEL.search(value)
    return match.group(1).upper() if match else ""


def choose_model_size(
    *,
    metadata_size_label: str,
    model_name: str,
    filename: str,
    parameter_count: int | None,
    calculated_formatter,
) -> tuple[str, str]:
    label = _valid_metadata_size_label(metadata_size_label)
    if label:
        return label, "metadata: general.size_label"
    label = _label_from_text(model_name)
    if label:
        return label, "metadata name heuristic"
    label = _label_from_text(Path(filename).stem)
    if label:
        return label, "filename heuristic"
    if parameter_count is not None:
        return calculated_formatter(parameter_count), "calculated"
    return "未知", "unknown: 无可靠规模信息"


def role_from_metadata(
    *, general_type: str, architecture: str, filename: str,
    tensor_count: int,
) -> tuple[ModelFileRole, str]:
    kind = general_type.strip().casefold()
    arch = architecture.strip().casefold()
    if kind in {"lora", "adapter"}:
        return ModelFileRole.ADAPTER, "metadata: general.type"
    if kind in {"embedding", "reranker", "rerank"}:
        return ModelFileRole.EMBEDDING, "metadata: general.type"
    if kind in {"mmproj", "projector"} or arch in {"clip", "vision", "whisper-encoder"}:
        return ModelFileRole.MMPROJ, "metadata"
    if kind in {"draft", "mtp"} or arch.endswith("-assistant"):
        return ModelFileRole.DRAFT, "metadata"
    if kind in {"control-vector", "control"}:
        return ModelFileRole.CONTROL, "metadata: general.type"
    if kind in {"vocab", "tokenizer"}:
        return ModelFileRole.AUXILIARY, "metadata: general.type"
    if kind == "model" or (arch and tensor_count > 0):
        return ModelFileRole.PRIMARY, "metadata"

    stem = Path(filename).stem.casefold()
    if stem.startswith("mmproj-") or "mmproj" in stem:
        return ModelFileRole.MMPROJ, "filename heuristic"
    if stem.startswith("mtp-") or re.search(r"(?:^|[-_.])draft(?:[-_.]|$)", stem):
        return ModelFileRole.DRAFT, "filename heuristic"
    if re.search(r"(?:^|[-_.])(lora|adapter)(?:[-_.]|$)", stem):
        return ModelFileRole.ADAPTER, "filename heuristic"
    if "control-vector" in stem or "control_vector" in stem:
        return ModelFileRole.CONTROL, "filename heuristic"
    if re.search(r"(?:^|[-_.])(embed|embedding|rerank|reranker)(?:[-_.]|$)", stem):
        return ModelFileRole.EMBEDDING, "filename heuristic"
    return ModelFileRole.UNKNOWN, "unknown"


def model_role_from_reference(role: ReferenceRole) -> ModelFileRole:
    return {
        ReferenceRole.PRIMARY: ModelFileRole.PRIMARY,
        ReferenceRole.MMPROJ: ModelFileRole.MMPROJ,
        ReferenceRole.DRAFT: ModelFileRole.DRAFT,
        ReferenceRole.ADAPTER: ModelFileRole.ADAPTER,
        ReferenceRole.CONTROL: ModelFileRole.CONTROL,
        ReferenceRole.AUXILIARY: ModelFileRole.AUXILIARY,
    }.get(role, ModelFileRole.UNKNOWN)


def is_hidden_auxiliary(role: ModelFileRole) -> bool:
    return role in {
        ModelFileRole.MMPROJ,
        ModelFileRole.DRAFT,
        ModelFileRole.ADAPTER,
        ModelFileRole.CONTROL,
        ModelFileRole.AUXILIARY,
    }
