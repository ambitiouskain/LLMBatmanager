from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

from llmbatdesk.resources import bundled_resource_path
from llmbatdesk.settings import default_data_dir


def test_source_resource_path_resolves_project_asset() -> None:
    path = bundled_resource_path("assets/LLMBatDesk.ico")
    assert path.name == "LLMBatDesk.ico"
    assert path.is_file()


def test_windows_icon_contains_common_multisize_layers() -> None:
    icon = Image.open("assets/LLMBatDesk.ico")
    sizes = set(icon.info.get("sizes", ()))
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= sizes


def test_onefile_resource_path_is_read_only_extraction_location(
    monkeypatch, tmp_path: Path
) -> None:
    extraction = tmp_path / "_MEI123"
    monkeypatch.setattr(sys, "_MEIPASS", str(extraction), raising=False)
    assert bundled_resource_path("assets/readme.txt") == extraction / "assets/readme.txt"


def test_persistent_data_ignores_meipass_and_uses_localappdata(
    monkeypatch, tmp_path: Path
) -> None:
    extraction = tmp_path / "_MEI456"
    local = tmp_path / "LocalAppData"
    monkeypatch.setattr(sys, "_MEIPASS", str(extraction), raising=False)
    monkeypatch.delenv("LLMBATDESK_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    data = default_data_dir()
    assert data == local / "LLMBatDesk"
    assert extraction not in data.parents


def test_onefile_spec_keeps_manifest_icon_and_no_console() -> None:
    spec = Path("LLMBatDesk-OneFile.spec").read_text(encoding="utf-8")
    assert 'name="LLMBatDesk-Portable"' in spec
    assert 'manifest="windows_dpi.manifest"' in spec
    assert 'icon="assets/LLMBatDesk.ico"' in spec
    assert "console=False" in spec
    assert "COLLECT(" not in spec


def test_installer_is_per_user_and_preserves_local_app_data() -> None:
    script = Path("installer/LLMBatDesk.iss").read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in script
    assert r"DefaultDirName={localappdata}\Programs\LLMBatDesk" in script
    assert "[UninstallDelete]" not in script
    assert "uninsdelete" not in script.casefold()
    assert "是否同时删除用户配置、数据库和日志？" in script
    assert "DelTree" in script
