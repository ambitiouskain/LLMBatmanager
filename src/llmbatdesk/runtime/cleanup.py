from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class StorageStats:
    data_dir: Path
    total_size: int
    database_size: int
    log_count: int
    log_size: int
    history_count: int
    temporary_size: int


@dataclass(frozen=True)
class CleanupResult:
    deleted_count: int = 0
    freed_bytes: int = 0


def _files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def directory_size(root: Path) -> int:
    total = 0
    for path in _files(root):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def storage_stats(data_dir: Path, database_path: Path, history_count: int) -> StorageStats:
    logs = _files(data_dir / "logs")
    log_size = sum(_safe_size(path) for path in logs)
    return StorageStats(
        data_dir=data_dir,
        total_size=directory_size(data_dir),
        database_size=_safe_size(database_path),
        log_count=len(logs),
        log_size=log_size,
        history_count=history_count,
        temporary_size=directory_size(data_dir / "temporary"),
    )


def cleanup_logs(
    log_dir: Path,
    *,
    retention_days: int | None,
    max_total_bytes: int | None,
    max_count: int | None,
    protected_paths: set[Path] | None = None,
    delete_all: bool = False,
) -> CleanupResult:
    protected = {path.resolve() for path in (protected_paths or set())}
    entries: list[tuple[Path, int, float]] = []
    protected_count = 0
    protected_size = 0
    for path in _files(log_dir):
        try:
            stat = path.stat()
            if path.resolve() in protected:
                protected_count += 1
                protected_size += stat.st_size
            else:
                entries.append((path, stat.st_size, stat.st_mtime))
        except OSError:
            continue
    entries.sort(key=lambda item: item[2])
    now = datetime.now(timezone.utc)
    cutoff = (
        now - timedelta(days=max(0, retention_days))
        if retention_days is not None else None
    )
    selected: set[Path] = set()
    if delete_all:
        selected.update(path for path, _size, _mtime in entries)
    elif cutoff is not None:
        selected.update(
            path for path, _size, mtime in entries
            if datetime.fromtimestamp(mtime, timezone.utc) < cutoff
        )
    remaining = [item for item in entries if item[0] not in selected]
    if max_count is not None:
        allowed_unprotected = max(0, max_count - protected_count)
        while len(remaining) > allowed_unprotected:
            selected.add(remaining.pop(0)[0])
    if max_total_bytes is not None:
        total = protected_size + sum(size for _path, size, _mtime in remaining)
        while remaining and total > max(0, max_total_bytes):
            path, size, _mtime = remaining.pop(0)
            selected.add(path)
            total -= size
    freed = 0
    deleted = 0
    for path, size, _mtime in entries:
        if path not in selected:
            continue
        try:
            path.unlink()
            deleted += 1
            freed += size
        except OSError:
            pass
    return CleanupResult(deleted, freed)


def cleanup_temporary_files(
    temporary_dir: Path, protected_paths: set[Path] | None = None
) -> CleanupResult:
    protected = {path.resolve() for path in (protected_paths or set())}
    deleted = freed = 0
    for path in _files(temporary_dir):
        try:
            if path.resolve() in protected:
                continue
            size = path.stat().st_size
            path.unlink()
            deleted += 1
            freed += size
        except OSError:
            pass
    return CleanupResult(deleted, freed)


def delete_user_data_tree(data_dir: Path) -> None:
    resolved = data_dir.resolve()
    executable = Path(sys.executable).resolve()
    if resolved == executable or resolved in executable.parents:
        raise ValueError("拒绝删除包含当前运行程序的数据目录")
    if resolved == Path(resolved.anchor):
        raise ValueError("拒绝删除磁盘根目录")
    if resolved.exists():
        shutil.rmtree(resolved)


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
