from __future__ import annotations

from pathlib import Path


class QtRecycleBin:
    """Windows recycle-bin adapter with no permanent-delete fallback."""

    def move_to_trash(self, path: Path) -> None:
        # Keep Qt file-operation code lazy: normal service startup and every
        # non-destructive removal path do not import or initialize it.
        from PySide6.QtCore import QFile

        result = QFile.moveToTrash(str(path))
        success = bool(result[0]) if isinstance(result, tuple) else bool(result)
        if not success:
            raise OSError(
                "Windows 回收站操作失败；原脚本文件保持不变。"
            )
