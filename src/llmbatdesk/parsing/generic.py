from __future__ import annotations

from pathlib import Path

from ..domain.models import Backend, Evidence, ParsedScript
from .common import StaticContext


def parse_generic(result: ParsedScript, context: StaticContext) -> None:
    result.backend = Backend.GENERIC
    for line, tokens in context.commands:
        command = tokens[0].casefold()
        if command in {"if", "for", "goto", "call", "start", "pause", "exit", "timeout"}:
            continue
        result.executable = tokens[0]
        for index, token in enumerate(tokens):
            low = token.casefold()
            if low == "--port" and index + 1 < len(tokens) and tokens[index + 1].isdigit():
                result.configured_port = int(tokens[index + 1])
            elif low.startswith("--port=") and low.split("=", 1)[1].isdigit():
                result.configured_port = int(low.split("=", 1)[1])
        result.evidence.append(Evidence(
            line_number=line.line_number, source_line=line.source,
            normalized_token=Path(tokens[0]).name, field="executable",
            reason="通用模式下检测到首个可执行命令",
        ))
        break
    result.environment = context.environment.copy()

