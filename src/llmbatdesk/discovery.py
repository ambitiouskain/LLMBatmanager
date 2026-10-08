from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterable

from .domain.models import ScriptRecord
from .hashing import fingerprint
from .parsing import parse_script
from .runnability import validate_runnability


def canonical_path(path: Path) -> str:
    return str(path.resolve()).casefold()


class ScriptScanner:
    def __init__(self, exclusions: Iterable[str] = ()) -> None:
        self.exclusions = tuple(exclusions)

    def scan(
        self, roots: Iterable[Path], individual: Iterable[Path] = (),
        ignored_paths: Iterable[str] = (),
    ) -> list[ScriptRecord]:
        ignored = {str(value).casefold() for value in ignored_paths}
        paths: dict[str, Path] = {}
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for path in root.rglob("*"):
                if path.is_file() and path.suffix.casefold() in {".bat", ".cmd"}:
                    relative = path.relative_to(root)
                    key = canonical_path(path)
                    if not self._excluded(relative) and key not in ignored:
                        paths.setdefault(key, path.resolve())
        for path in individual:
            if path.exists() and path.is_file() and path.suffix.casefold() in {".bat", ".cmd"}:
                key = canonical_path(path)
                if key not in ignored:
                    paths.setdefault(key, path.resolve())
        records: list[ScriptRecord] = []
        for path in sorted(paths.values(), key=lambda item: str(item).casefold()):
            parsed = parse_script(path)
            records.append(ScriptRecord(
                fingerprint=fingerprint(path),
                parsed=parsed,
                runnability=validate_runnability(parsed),
            ))
        return records

    def _excluded(self, relative: Path) -> bool:
        normalized = relative.as_posix()
        return any(
            fnmatch.fnmatch(normalized, pattern)
            or any(fnmatch.fnmatch(part, pattern) for part in relative.parts[:-1])
            for pattern in self.exclusions
        )


def likely_moves(old: Iterable[ScriptRecord], new: Iterable[ScriptRecord]) -> dict[str, str]:
    old_by_hash: dict[str, list[str]] = {}
    new_by_hash: dict[str, list[str]] = {}
    for record in old:
        old_by_hash.setdefault(record.fingerprint.sha256, []).append(record.fingerprint.canonical_path)
    for record in new:
        new_by_hash.setdefault(record.fingerprint.sha256, []).append(record.fingerprint.canonical_path)
    moves: dict[str, str] = {}
    for digest in old_by_hash.keys() & new_by_hash.keys():
        removed = set(old_by_hash[digest]) - set(new_by_hash[digest])
        added = set(new_by_hash[digest]) - set(old_by_hash[digest])
        if len(removed) == len(added) == 1:
            moves[next(iter(removed))] = next(iter(added))
    return moves
