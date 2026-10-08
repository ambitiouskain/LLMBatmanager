from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ..domain.models import Backend, Evidence, ParseConfidence, ParsedScript, PortSource
from ..hashing import sha256_bytes
from .common import analyze_lines
from .generic import parse_generic
from .llama_cpp import parse_llama
from .ollama import parse_ollama
from .tokenizer import decode_batch, logical_lines


def parse_script(path: Path) -> ParsedScript:
    data = path.read_bytes()
    result = parse_script_bytes(data, path)
    result.modified_at = datetime.fromtimestamp(path.stat().st_mtime)
    return result


def parse_script_bytes(data: bytes, path: Path = Path("script.bat")) -> ParsedScript:
    text, encoding = decode_batch(data)
    newline = "\r\n" if "\r\n" in text else "\n"
    result = ParsedScript(
        path=path, encoding=encoding, newline=newline, raw_text=text,
        content_hash=sha256_bytes(data),
    )
    context = analyze_lines(logical_lines(text))
    result.warnings.extend(context.warnings)
    result.dynamic_reasons.extend(context.dynamic_reasons)
    result.uses_start, result.uses_call = context.uses_start, context.uses_call
    result.interactive_commands = context.interactive_commands
    if not parse_llama(result, context) and not parse_ollama(result, context):
        parse_generic(result, context)
    _classify_interactive_commands(result, context)
    _locate_port_source(result, text, context)
    _derive_addresses(result)
    if result.dynamic_reasons:
        result.confidence = (
            ParseConfidence.PARTIAL if result.backend != Backend.GENERIC or result.executable
            else ParseConfidence.UNKNOWN
        )
        result.warnings.append("脚本可启动，但只能部分静态解析")
    elif result.backend in {Backend.LLAMA_CPP, Backend.OLLAMA}:
        result.confidence = ParseConfidence.FULL
    elif result.executable:
        result.confidence = ParseConfidence.PARTIAL
        result.warnings.append("后端未受一等支持；按通用脚本处理")
    else:
        result.confidence = ParseConfidence.UNKNOWN
    return result


def _classify_interactive_commands(result: ParsedScript, context: object) -> None:
    """Separate a terminal-keeping trailing PAUSE from real launch input."""
    if "pause" not in result.interactive_commands or result.executable is None:
        return
    if any("分支" in reason or "GOTO" in reason for reason in result.dynamic_reasons):
        return
    executable_name = Path(result.executable).name.casefold()
    commands = context.commands
    main_index = next(
        (
            index for index, (_line, tokens) in enumerate(commands)
            if any(Path(token).name.casefold() == executable_name for token in tokens)
        ),
        None,
    )
    if main_index is None:
        return
    following = commands[main_index + 1:]
    if not following:
        return
    if all(
        tokens and tokens[0].casefold() == "pause"
        for _line, tokens in following
    ):
        result.harmless_trailing_pause = True
        result.interactive_commands = [
            command for command in result.interactive_commands if command != "pause"
        ]
        result.warnings.append(
            "检测到服务器命令后的尾部 pause；它只用于服务器退出后保留控制台"
        )


def _locate_port_source(result: ParsedScript, text: str, context: object) -> None:
    if result.configured_port is None or result.dynamic_reasons:
        return
    value = str(result.configured_port)
    patterns = [
        (re.compile(rf"(?im)(--port\s+)(?P<v>{re.escape(value)})(?=\s|\^|$)"), "argument"),
        (re.compile(rf"(?im)(--port=)(?P<v>{re.escape(value)})(?=\s|\^|$)"), "argument_equals"),
        (re.compile(rf"(?im)(?<!\S)(-p\s+)(?P<v>{re.escape(value)})(?=\s|\^|$)"), "argument"),
        (re.compile(rf'(?im)(set\s+"?(?:PORT|LLAMA_PORT)=)(?P<v>{re.escape(value)})(?="?[\r\n])'),
         "environment"),
    ]
    matches = [(match, style) for pattern, style in patterns for match in pattern.finditer(text)]
    if len(matches) != 1:
        if len(matches) > 1:
            result.warnings.append("检测到多个端口来源，已禁用临时覆盖")
        return
    match, style = matches[0]
    line_number = text.count("\n", 0, match.start("v")) + 1
    result.port_source = PortSource(
        line_number=line_number, start=match.start("v"), end=match.end("v"),
        original=value, style=style,
    )
    result.evidence.append(Evidence(
        line_number=line_number,
        source_line=re.sub(
            r'(?i)(--api-key(?:=|\s+))(?:"[^"]*"|\S+)',
            lambda match: match.group(1) + "<已隐藏>",
            text.splitlines()[line_number - 1],
        ),
        normalized_token=value,
        field="port_source",
        reason="唯一、静态的端口字面量，可安全临时覆盖",
    ))


def _derive_addresses(result: ParsedScript, port: int | None = None) -> None:
    actual_port = port or result.configured_port
    if actual_port is None:
        return
    bind_host = result.bind_host or ("127.0.0.1" if result.backend == Backend.LLAMA_CPP else "127.0.0.1")
    result.bind_host = bind_host
    client_host = "127.0.0.1" if bind_host in {"0.0.0.0", "::", "[::]"} else bind_host
    path = "/v1" if result.backend == Backend.LLAMA_CPP else ""
    result.bind_address = f"http://{bind_host}:{actual_port}{path}"
    result.client_address = f"http://{client_host}:{actual_port}{path}"


def runtime_api_address(parsed: ParsedScript, actual_port: int) -> str:
    host = "127.0.0.1" if parsed.bind_host in {None, "0.0.0.0", "::", "[::]"} else parsed.bind_host
    suffix = "/v1" if parsed.backend == Backend.LLAMA_CPP else ""
    return f"http://{host}:{actual_port}{suffix}"
