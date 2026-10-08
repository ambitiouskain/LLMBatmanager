from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tokenizer import LogicalLine, tokenize
from .variables import expand_static

SET_RE = re.compile(r'^\s*@?set\s+(?:"([^"=]+)=(.*)"|([^=\s]+)=(.*))\s*$', re.IGNORECASE)
DELAYED_VARIABLE_RE = re.compile(r"![A-Za-z_][A-Za-z0-9_]*!")


def _unquoted_metacharacters(value: str) -> set[str]:
    """Return active batch metacharacters, ignoring quotes and caret escapes."""
    found: set[str] = set()
    quoted = False
    index = 0
    while index < len(value):
        character = value[index]
        if character == "^" and index + 1 < len(value):
            index += 2
            continue
        if character == '"':
            quoted = not quoted
        elif not quoted and character in "&|()":
            found.add(character)
        index += 1
    return found


@dataclass
class StaticContext:
    environment: dict[str, str] = field(default_factory=dict)
    commands: list[tuple[LogicalLine, list[str]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dynamic_reasons: list[str] = field(default_factory=list)
    uses_call: bool = False
    uses_start: bool = False
    safe_port_assignments: dict[str, tuple[LogicalLine, str]] = field(default_factory=dict)
    interactive_commands: list[str] = field(default_factory=list)


def analyze_lines(lines: list[LogicalLine]) -> StaticContext:
    context = StaticContext()
    for line in lines:
        stripped = line.text.strip()
        low = stripped.casefold()
        if DELAYED_VARIABLE_RE.search(stripped):
            context.dynamic_reasons.append(f"第 {line.line_number} 行可能使用延迟变量展开")
        if not stripped or low.startswith(("rem ", "::", "@echo", "echo ")):
            continue
        if re.match(r"^\s*@?set\s+/p\b", stripped, re.I):
            context.interactive_commands.append("set /p")
            context.dynamic_reasons.append(f"第 {line.line_number} 行使用 set /p 动态输入")
            continue
        if re.match(r"^\s*@?pause(?:\s|$)", stripped, re.I):
            context.interactive_commands.append("pause")
        if re.match(r"^\s*@?choice(?:\s|$)", stripped, re.I):
            context.interactive_commands.append("choice")
        match = SET_RE.match(stripped)
        if match:
            name = (match.group(1) or match.group(3) or "").upper()
            value = match.group(2) if match.group(1) is not None else match.group(4)
            expanded, unresolved = expand_static(value or "", context.environment)
            context.environment[name] = expanded
            if unresolved:
                context.dynamic_reasons.append(
                    f"第 {line.line_number} 行变量 {name} 依赖未知变量：{', '.join(unresolved)}"
                )
            if name in {"PORT", "LLAMA_PORT"} and name in context.safe_port_assignments:
                context.dynamic_reasons.append(
                    f"第 {line.line_number} 行重复设置端口变量 {name}"
                )
            if name in {"PORT", "LLAMA_PORT"} and expanded.isdigit() and not unresolved:
                context.safe_port_assignments[name] = (line, expanded)
            continue
        metacharacters = _unquoted_metacharacters(stripped)
        if metacharacters:
            context.dynamic_reasons.append(
                f"第 {line.line_number} 行包含批处理控制运算符："
                + " ".join(sorted(metacharacters))
            )
        command_text = stripped.lstrip("@").lstrip()
        if re.match(r"(?i)^(?:if|for|else)\b", command_text):
            context.dynamic_reasons.append(
                f"第 {line.line_number} 行包含条件或循环分支"
            )
        if re.match(
            r"(?i)^(?:cmd(?:\.exe)?\s+/(?:c|k)\b|powershell(?:\.exe)?\b|pwsh(?:\.exe)?\b)",
            command_text,
        ):
            context.dynamic_reasons.append(
                f"第 {line.line_number} 行通过动态命令包装器启动"
            )
        if low.startswith("call ") or low.startswith("@call "):
            context.uses_call = True
            context.dynamic_reasons.append(f"第 {line.line_number} 行包含外部 CALL")
        if low.startswith("start ") or low.startswith("@start "):
            context.uses_start = True
            context.warnings.append("脚本使用 START，子进程可能脱离直接进程树")
            context.dynamic_reasons.append(f"第 {line.line_number} 行使用 START，进程所有权可能不完整")
        if low.startswith("goto ") or stripped.startswith(":"):
            context.dynamic_reasons.append(f"第 {line.line_number} 行包含标签或 GOTO 分支")
        if re.search(r"(^|[^2])([12]?>|<|>>)", stripped):
            context.warnings.append(f"第 {line.line_number} 行包含重定向")
        tokens, balanced = tokenize(stripped.lstrip("@"))
        if not balanced:
            context.dynamic_reasons.append(f"第 {line.line_number} 行引号不完整")
        expanded_tokens: list[str] = []
        for token in tokens:
            expanded, unresolved = expand_static(token, context.environment)
            expanded_tokens.append(expanded)
            if unresolved:
                context.dynamic_reasons.append(
                    f"第 {line.line_number} 行包含未知变量：{', '.join(unresolved)}"
                )
        if expanded_tokens:
            context.commands.append((line, expanded_tokens))
    context.dynamic_reasons = list(dict.fromkeys(context.dynamic_reasons))
    context.warnings = list(dict.fromkeys(context.warnings))
    context.interactive_commands = list(dict.fromkeys(context.interactive_commands))
    return context
