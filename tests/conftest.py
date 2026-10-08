from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from llmbatdesk.qt.app import configure_high_dpi

configure_high_dpi()


@pytest.fixture
def llama_text() -> str:
    return (
        '@echo off\r\n'
        '"C:\\Program Files\\llama\\llama-server.exe" ^\r\n'
        ' --model "C:\\模型\\Gemma 4\\model.gguf" ^\r\n'
        ' --host 0.0.0.0 --port 8080 --ctx-size 32768 -ngl 99 ^\r\n'
        ' --flash-attn on --temp 0.85 --top-p 0.95\r\n'
    )


@pytest.fixture
def script_file(tmp_path: Path, llama_text: str) -> Path:
    path = tmp_path / "中文 启动.bat"
    path.write_bytes(llama_text.encode("utf-8"))
    return path
