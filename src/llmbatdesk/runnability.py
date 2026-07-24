from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable

from .domain.models import (
    Backend, ParseConfidence, ParsedScript, RunnabilityReport, RunnabilityStatus,
)


def _dynamic(value: str | None) -> bool:
    return bool(value and ("%" in value or "!" in value))


def resolve_executable(
    executable: str | None,
    which: Callable[[str], str | None] = shutil.which,
    base_dir: Path | None = None,
) -> tuple[str | None, bool | None]:
    if not executable:
        return None, None
    expanded = os.path.expandvars(executable)
    if _dynamic(expanded):
        return None, None
    path = Path(expanded)
    explicit = path.is_absolute() or any(separator in expanded for separator in ("\\", "/"))
    if explicit:
        if not path.is_absolute() and base_dir:
            path = base_dir / path
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        return str(resolved), resolved.is_file()
    located = which(expanded)
    return located, bool(located and Path(located).is_file())


def validate_runnability(
    parsed: ParsedScript,
    which: Callable[[str], str | None] = shutil.which,
) -> RunnabilityReport:
    resolved, executable_exists = resolve_executable(parsed.executable, which, parsed.path.parent)
    model_exists: bool | None = None
    warnings: list[str] = []
    if parsed.model_path:
        if _dynamic(parsed.model_path):
            warnings.append(f"模型路径无法静态解析：\n{parsed.model_path}")
        else:
            model_path = Path(os.path.expandvars(parsed.model_path))
            if not model_path.is_absolute():
                model_path = parsed.path.parent / model_path
            model_exists = model_path.is_file()
            if not model_exists:
                warnings.append(f"模型文件不存在：\n{parsed.model_path}")
    if parsed.executable and executable_exists is False:
        warnings.insert(0, f"可执行文件不存在：\n{parsed.executable}")
    if _dynamic(parsed.executable) or _dynamic(parsed.model_path):
        return RunnabilityReport(
            status=RunnabilityStatus.DYNAMIC_PATH,
            launch_allowed=parsed.confidence != ParseConfidence.FULL,
            resolved_executable=resolved,
            executable_exists=executable_exists,
            model_exists=model_exists,
            warnings=warnings,
        )
    if parsed.backend in {Backend.LLAMA_CPP, Backend.OLLAMA}:
        missing_executable = executable_exists is False
        missing_model = parsed.backend == Backend.LLAMA_CPP and model_exists is False
        if missing_executable and missing_model:
            status = RunnabilityStatus.EXECUTABLE_AND_MODEL_MISSING
        elif missing_executable:
            status = RunnabilityStatus.EXECUTABLE_MISSING
        elif missing_model:
            status = RunnabilityStatus.MODEL_MISSING
        else:
            status = RunnabilityStatus.RUNNABLE
        return RunnabilityReport(
            status=status,
            launch_allowed=not (missing_executable or missing_model),
            resolved_executable=resolved,
            executable_exists=executable_exists,
            model_exists=model_exists,
            warnings=warnings,
        )
    explicit_generic = bool(
        parsed.executable
        and any(separator in parsed.executable for separator in ("\\", "/"))
    )
    if explicit_generic and executable_exists is False:
        return RunnabilityReport(
            status=RunnabilityStatus.EXECUTABLE_MISSING,
            launch_allowed=False,
            resolved_executable=resolved,
            executable_exists=False,
            model_exists=model_exists,
            warnings=warnings,
        )
    return RunnabilityReport(
        status=RunnabilityStatus.UNKNOWN_BACKEND,
        launch_allowed=True,
        resolved_executable=resolved,
        executable_exists=executable_exists,
        model_exists=model_exists,
        warnings=[*warnings, "通用后端无法完整验证可运行性；启动前请检查脚本内容。"],
    )
