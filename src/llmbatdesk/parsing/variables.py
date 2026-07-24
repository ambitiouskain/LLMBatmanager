from __future__ import annotations

import re

VARIABLE_RE = re.compile(r"%([^%]+)%")


def expand_static(value: str, variables: dict[str, str]) -> tuple[str, list[str]]:
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).upper()
        if name in variables:
            return variables[name]
        unresolved.append(name)
        return match.group(0)

    return VARIABLE_RE.sub(replace, value), unresolved

