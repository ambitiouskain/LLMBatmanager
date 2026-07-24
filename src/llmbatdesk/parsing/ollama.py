from __future__ import annotations

from pathlib import Path

from ..domain.models import Backend, Evidence, ParsedScript
from .common import StaticContext

OLLAMA_ENV = {
    "OLLAMA_HOST", "OLLAMA_KEEP_ALIVE", "OLLAMA_NUM_PARALLEL",
    "OLLAMA_MAX_LOADED_MODELS", "CUDA_VISIBLE_DEVICES",
}


def parse_ollama(result: ParsedScript, context: StaticContext) -> bool:
    candidate = None
    for line, tokens in context.commands:
        index = next((i for i, t in enumerate(tokens) if Path(t).name.casefold() in
                      {"ollama", "ollama.exe"}), None)
        if index is not None and index + 1 < len(tokens) and tokens[index + 1].casefold() in {
            "serve", "run", "stop"
        }:
            candidate = line, tokens, index
            break
    if not candidate:
        return False
    line, tokens, index = candidate
    action = tokens[index + 1].casefold()
    result.backend = Backend.OLLAMA
    result.executable = tokens[index]
    result.parameters["ollama_action"] = action
    if action in {"run", "stop"} and len(tokens) > index + 2:
        result.model_name = tokens[index + 2]
    elif action == "serve":
        result.warnings.append("服务脚本；未指定固定模型")
    result.environment = {k: v for k, v in context.environment.items() if k in OLLAMA_ENV}
    host = result.environment.get("OLLAMA_HOST", "127.0.0.1:11434")
    host = host.removeprefix("http://").removeprefix("https://")
    if ":" in host:
        result.bind_host, raw_port = host.rsplit(":", 1)
        if raw_port.isdigit():
            result.configured_port = int(raw_port)
    else:
        result.bind_host, result.configured_port = host, 11434
    result.evidence.append(Evidence(
        line_number=line.line_number, source_line=line.source,
        normalized_token=f"ollama {action}", field="backend",
        reason="识别 Ollama 子命令",
    ))
    return True

