from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LogicalLine:
    text: str
    source: str
    line_number: int
    start_offset: int


TOKEN_RE = re.compile(r'"(?:[^"]|"")*"|[^\s]+')


def decode_batch(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16" if data.startswith(b"\xff\xfe") else "utf-16-be"
        return data.decode(encoding), encoding
    for encoding in ("utf-8", "gb18030", "cp932", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8"


def logical_lines(text: str) -> list[LogicalLine]:
    physical = text.splitlines(keepends=True)
    result: list[LogicalLine] = []
    buffer = ""
    source = ""
    first_line = 1
    start_offset = 0
    offset = 0
    for number, raw in enumerate(physical, 1):
        body = raw.rstrip("\r\n")
        if not buffer:
            first_line, start_offset = number, offset
        source += raw
        stripped = body.rstrip()
        continued = stripped.endswith("^") and not stripped.endswith("^^")
        if continued:
            buffer += stripped[:-1] + " "
        else:
            buffer += body
            result.append(LogicalLine(buffer, source.rstrip("\r\n"), first_line, start_offset))
            buffer, source = "", ""
        offset += len(raw)
    if buffer or source:
        result.append(LogicalLine(buffer, source, first_line, start_offset))
    return result


def tokenize(command: str) -> tuple[list[str], bool]:
    if command.count('"') % 2:
        return TOKEN_RE.findall(command), False
    return [unquote(item) for item in TOKEN_RE.findall(command)], True


def unquote(value: str) -> str:
    value = value.strip()
    return value[1:-1].replace('""', '"') if len(value) >= 2 and value[0] == value[-1] == '"' else value

