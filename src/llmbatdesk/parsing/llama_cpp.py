from __future__ import annotations

from pathlib import Path
import re

from ..domain.models import Backend, Evidence, ParsedScript
from .common import StaticContext

VALUE_OPTIONS = {
    "--model": "model_path", "-m": "model_path",
    "--host": "bind_host", "--port": "configured_port",
    "--ctx-size": "context_size", "-c": "context_size",
    "--n-gpu-layers": "gpu_layers", "--gpu-layers": "gpu_layers",
    "-ngl": "gpu_layers",
    "--batch-size": "batch_size", "-b": "batch_size",
    "--ubatch-size": "micro_batch_size", "-ub": "micro_batch_size",
    "--parallel": "parallel_slots", "-np": "parallel_slots",
    "--threads": "threads", "-t": "threads",
    "--flash-attn": "flash_attention", "--cache-type-k": "cache_type_k",
    "--cache-type-v": "cache_type_v", "--temp": "temperature",
    "--top-p": "top_p", "--min-p": "min_p", "--repeat-penalty": "repeat_penalty",
    "--seed": "seed", "--chat-template": "chat_template",
    "--api-key": "api_key", "--draft-model": "draft_model",
}
FLAG_OPTIONS = {
    "--jinja": "jinja", "--no-context-shift": "context_shift_disabled",
    "--cont-batching": "continuous_batching", "--no-mmap": "mmap_disabled",
    "--mlock": "memory_lock",
}


def _is_llama(tokens: list[str]) -> bool:
    names = [Path(token).name.casefold() for token in tokens[:4]]
    if "llama-server.exe" in names or "llama-server" in names:
        return True
    if "server.exe" in names or "server" in names:
        lowers = {t.casefold().split("=", 1)[0] for t in tokens}
        return bool(lowers & {
            "--model", "-m", "--ctx-size", "-c",
            "--n-gpu-layers", "--gpu-layers", "-ngl",
        })
    return False


def parse_llama(result: ParsedScript, context: StaticContext) -> bool:
    candidate = next(((line, tokens) for line, tokens in context.commands if _is_llama(tokens)), None)
    if not candidate:
        return False
    line, tokens = candidate
    result.backend = Backend.LLAMA_CPP
    exe_index = next(
        (i for i, token in enumerate(tokens) if Path(token).name.casefold() in
         {"llama-server.exe", "llama-server", "server.exe", "server"}), 0
    )
    result.executable = tokens[exe_index]
    i = exe_index + 1
    known_indexes: set[int] = {exe_index}
    while i < len(tokens):
        raw = tokens[i]
        key, equal, inline = raw.partition("=")
        low = key.casefold()
        if low in FLAG_OPTIONS:
            result.parameters[FLAG_OPTIONS[low]] = True
            result.evidence.append(Evidence(
                line_number=line.line_number, source_line=_redact_source(line.source),
                normalized_token=raw, field=FLAG_OPTIONS[low], reason="识别 llama.cpp 布尔开关",
            ))
            known_indexes.add(i)
            i += 1
            continue
        if low in VALUE_OPTIONS:
            original_argument = raw
            if equal:
                value = inline
                known_indexes.add(i)
            elif i + 1 < len(tokens):
                value = tokens[i + 1]
                original_argument = f"{raw} {value}"
                known_indexes.update({i, i + 1})
                i += 1
            else:
                result.warnings.append(f"参数 {key} 缺少值")
                i += 1
                continue
            field = VALUE_OPTIONS[low]
            if field == "api_key":
                result.has_api_key = True
            elif field == "gpu_layers":
                symbolic = value.casefold()
                if symbolic in {"auto", "all"}:
                    result.gpu_layers = symbolic
                else:
                    try:
                        result.gpu_layers = int(value)
                    except ValueError:
                        result.dynamic_reasons.append(
                            f"{key} 的值必须是整数、auto 或 all"
                        )
            elif field in {"configured_port", "context_size"}:
                try:
                    setattr(result, field, int(value))
                except ValueError:
                    result.dynamic_reasons.append(f"{key} 的值无法静态解析为整数")
            elif field == "model_path":
                result.model_path = value
                result.model_name = Path(value).name
                result.model_exists = Path(value).exists() if "%" not in value else None
            elif field == "bind_host":
                result.bind_host = value
            else:
                result.parameters[field] = _coerce(value)
            result.evidence.append(Evidence(
                line_number=line.line_number, source_line=_redact_source(line.source),
                normalized_token=(
                    f"{key}=<已隐藏>" if field == "api_key"
                    else original_argument if field == "gpu_layers"
                    else f"{key}={value}"
                ),
                field=field, reason="识别 llama.cpp 命令行参数",
            ))
        i += 1
    result.other_arguments = [
        token for index, token in enumerate(tokens[exe_index + 1:], exe_index + 1)
        if index not in known_indexes and not token.casefold().startswith("--api-key=")
    ]
    return True


def _redact_source(source: str) -> str:
    return re.sub(
        r'(?i)(--api-key(?:=|\s+))(?:"[^"]*"|\S+)',
        lambda match: match.group(1) + "<已隐藏>",
        source,
    )


def _coerce(value: str) -> str | int | float | bool:
    if value.casefold() in {"on", "true", "yes"}:
        return True
    if value.casefold() in {"off", "false", "no"}:
        return False
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value
