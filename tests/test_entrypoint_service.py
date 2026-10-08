from __future__ import annotations

import importlib
from pathlib import Path

from llmbatdesk.services import ApplicationService
from llmbatdesk.domain.models import ManagedLaunch, PortOccupant, ProcessIdentity, RuntimeState
from llmbatdesk.settings import AppSettings, SettingsStore
from llmbatdesk.storage import MetadataStore


def test_pyinstaller_entrypoint_import() -> None:
    module = importlib.import_module("llmbatdesk.__main__")
    assert callable(module.main)


def test_service_searches_metadata_and_model(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "x.bat").write_text("ollama run qwen2.5:14b", encoding="utf-8")
    data = tmp_path / "data"
    settings = SettingsStore(data)
    settings.save(AppSettings(roots=[str(scripts)]))
    service = ApplicationService(
        data_dir=data, settings_store=settings, metadata_store=MetadataStore(data / "db.sqlite")
    )
    records = service.rescan()
    metadata = service.metadata(records[0])
    metadata.tags = ["翻译"]
    metadata.notes = "中文表现"
    service.save_metadata(metadata)
    assert service.search("qwen") == records
    assert service.search("翻译") == records


def test_trust_invalidates_after_script_modification(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    path = scripts / "x.bat"
    path.write_text("ollama serve", encoding="utf-8")
    data = tmp_path / "data"
    settings = SettingsStore(data)
    settings.save(AppSettings(roots=[str(scripts)]))
    service = ApplicationService(
        data_dir=data, settings_store=settings, metadata_store=MetadataStore(data / "db.sqlite")
    )
    first = service.rescan()[0]
    service.trust(first)
    assert service.is_trusted(first)
    path.write_text("ollama serve\nrem changed", encoding="utf-8")
    second = service.rescan()[0]
    assert not service.is_trusted(second)


def test_duplicate_and_managed_copy_never_overwrite(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = scripts / "x.bat"
    source.write_text("ollama serve\r\n", encoding="utf-8", newline="")
    data = tmp_path / "data"
    settings = SettingsStore(data)
    settings.save(AppSettings(roots=[str(scripts)]))
    service = ApplicationService(
        data_dir=data, settings_store=settings, metadata_store=MetadataStore(data / "db.sqlite")
    )
    record = service.rescan()[0]
    copied, managed = tmp_path / "copy.bat", tmp_path / "managed.bat"
    service.duplicate_script(record, copied)
    service.duplicate_script(record, managed, managed=True)
    assert copied.read_bytes() == source.read_bytes()
    assert b"LLMBatDesk generated managed copy" in managed.read_bytes()
    import pytest
    with pytest.raises(FileExistsError):
        service.duplicate_script(record, copied)


def test_managed_port_occupant_is_correlated_by_pid_and_port(tmp_path: Path) -> None:
    class FakePort:
        def inspect(self, port: int):
            return PortOccupant(port=port, pid=42)
    class FakeProcess:
        current = ProcessIdentity(pid=42, create_time=1, executable="llama-server.exe")
        def identity(self, pid: int):
            return self.current if pid == 42 else None
        def children(self, _pid: int):
            return []
    data = tmp_path / "data"
    service = ApplicationService(
        data_dir=data, settings_store=SettingsStore(data),
        metadata_store=MetadataStore(data / "db.sqlite"), port_inspector=FakePort(),
        process_inspector=FakeProcess(),
    )
    service.launches["launch"] = ManagedLaunch(
        launch_id="launch", script_path="x.bat", script_hash="h",
        identity=ProcessIdentity(pid=42, create_time=1), configured_port=8080,
        server_identity=FakeProcess.current,
        actual_port=8080, log_path="x.log", verified=True,
        state=RuntimeState.PORT_LISTENING_API_NOT_READY,
    )
    from llmbatdesk.parsing import parse_script_bytes
    parsed = parse_script_bytes(b"llama-server.exe -m x.gguf --port 8080", Path("x.bat"))
    assert service.inspect_port(parsed).managed_launch_id == "launch"
