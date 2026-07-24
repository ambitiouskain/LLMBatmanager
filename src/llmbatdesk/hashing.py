from __future__ import annotations

import hashlib
from pathlib import Path

from .domain.models import ScriptFingerprint


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(path: Path) -> ScriptFingerprint:
    resolved = path.resolve()
    stat = resolved.stat()
    return ScriptFingerprint(
        canonical_path=str(resolved).casefold(),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=sha256_bytes(resolved.read_bytes()),
    )

