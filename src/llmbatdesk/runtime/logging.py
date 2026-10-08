from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from .. import __version__

SECRET_PATTERNS = [
    re.compile(r'(?i)(--api-key(?:=|\s+))("[^"]*"|\S+)'),
    re.compile(r'(?i)((?:OPENAI_API_KEY|API_KEY)\s*=\s*)("[^"]*"|[^\s\r\n]+)'),
    re.compile(r"(?i)(Authorization:\s*Bearer\s+)(\S+)"),
]
FAILURE_PATTERNS = {
    "模型文件不存在": re.compile(r"(?i)(model.*(?:not found|does not exist)|找不到.*模型)"),
    "可执行文件不存在": re.compile(r"(?i)(not recognized as an internal|executable.*not found)"),
    "端口已被占用": re.compile(r"(?i)(address already in use|port.*in use)"),
    "CUDA 初始化失败": re.compile(r"(?i)(CUDA.*(?:initialization|init).*(?:fail|error))"),
    "显存不足": re.compile(r"(?i)(out of memory|insufficient (?:vram|memory)|cuda.*oom)"),
    "模型格式无效": re.compile(r"(?i)(invalid.*model|unsupported architecture)"),
    "上下文分配失败": re.compile(r"(?i)(context.*(?:alloc|memory).*(?:fail|error))"),
}


def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda match: match.group(1) + "<已隐藏>", text)
    return text


def detect_failure(text: str) -> str | None:
    return next((name for name, pattern in FAILURE_PATTERNS.items() if pattern.search(text)), None)


class OperationLogger:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir

    def create(self, launch_info: Mapping[str, object]) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.log"
        lines = [f"LLMBatDesk {__version__}", f"时间: {datetime.now().isoformat()}"]
        lines.extend(f"{key}: {redact(str(value))}" for key, value in launch_info.items())
        path.write_text("\n".join(lines) + "\n\n", encoding="utf-8")
        return path

    def append(self, path: Path, stream: str, text: str) -> None:
        with path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(f"[{stream}] {redact(text)}")
