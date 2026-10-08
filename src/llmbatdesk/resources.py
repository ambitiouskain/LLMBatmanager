from __future__ import annotations

import sys
from pathlib import Path


def bundled_resource_path(relative_path: str) -> Path:
    """Return a read-only bundled resource path.

    Persistent application data must never be written through this function.
    In a PyInstaller one-file build the returned path can live under the
    temporary ``sys._MEIPASS`` extraction directory.
    """
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / relative_path
    return Path(__file__).resolve().parents[2] / relative_path
