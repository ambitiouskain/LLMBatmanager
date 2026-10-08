from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

from .domain.models import EditorMode, LaunchConfirmationMode, LaunchMode


def default_data_dir() -> Path:
    explicit = os.environ.get("LLMBATDESK_DATA_DIR")
    if explicit:
        return Path(explicit)
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "LLMBatDesk"


class AppSettings(BaseModel):
    roots: list[str] = Field(default_factory=list)
    individual_scripts: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=lambda: [".git", ".venv", "node_modules"])
    editor_path: str = ""
    editor_mode: EditorMode = EditorMode.SYSTEM_DEFAULT
    stop_timeout_seconds: float = 5.0
    api_startup_timeout_seconds: float = 120.0
    normal_check_interval_seconds: float = 30.0
    window_geometry: str = "1480x880"
    window_maximized: bool = False
    pane_positions: list[int] = Field(default_factory=lambda: [330, 1030])
    theme: str = "dark"
    detail_window_geometry: str = "900x650"
    qt_geometry: str = ""
    qt_window_state: str = ""
    qt_splitter_state: str = ""
    qt_selected_tab: int = 0
    qt_runtime_tab: int = 0
    qt_main_tab: int = 0
    qt_column_widths: dict[str, list[int]] = Field(default_factory=dict)
    last_selected_path: str = ""
    launch_mode: LaunchMode = LaunchMode.BACKGROUND
    launch_confirmation_mode: LaunchConfirmationMode = (
        LaunchConfirmationMode.FIRST_OR_CHANGED
    )
    log_retention_days: int | None = 30
    log_max_total_mb: int | None = 200
    log_max_count: int | None = 100
    log_auto_scroll: bool = True
    terminal_font_family: str = ""
    terminal_font_size: int = 11
    terminal_line_spacing_percent: int = 100
    model_library_enabled: bool = False
    model_library_allow_running_scan: bool = False
    model_library_column_widths: dict[str, list[int]] = Field(
        default_factory=dict
    )

    @field_validator(
        "model_library_enabled", "model_library_allow_running_scan",
        mode="before",
    )
    @classmethod
    def safe_extension_flags(cls, value):
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        # A malformed optional extension setting must not invalidate otherwise
        # usable core settings during an upgrade.
        return False

    @field_validator("model_library_column_widths", mode="before")
    @classmethod
    def safe_model_library_columns(cls, value):
        if not isinstance(value, dict):
            return {}
        result: dict[str, list[int]] = {}
        for key in ("scan_roots", "models", "references", "components"):
            widths = value.get(key)
            if isinstance(widths, list):
                result[key] = [
                    item for item in widths
                    if isinstance(item, int) and not isinstance(item, bool)
                    and 55 <= item <= 3000
                ][:16]
        return result

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_editor(cls, data):
        # 1.2.2 and older stored only editor_path. A non-empty legacy value
        # means the user had deliberately selected a custom editor.
        if (
            isinstance(data, dict)
            and data.get("editor_path")
            and "editor_mode" not in data
        ):
            data = {**data, "editor_mode": EditorMode.CUSTOM}
        return data

    @model_validator(mode="after")
    def clamp_terminal_appearance(self) -> "AppSettings":
        self.terminal_font_size = min(24, max(8, self.terminal_font_size))
        self.terminal_line_spacing_percent = min(
            200, max(80, self.terminal_line_spacing_percent)
        )
        return self


def resolve_editor_executable(value: str) -> Path | None:
    if not value.strip():
        return None
    path = Path(value)
    if path.is_file():
        return path.resolve()
    resolved = shutil.which(value)
    return Path(resolved).resolve() if resolved else None


class SettingsStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.path = self.data_dir / "settings.json"

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            return AppSettings.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)
