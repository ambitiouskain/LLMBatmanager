from __future__ import annotations

from pathlib import Path

from llmbatdesk.discovery import ScriptScanner, likely_moves
from llmbatdesk.domain.models import Metadata
from llmbatdesk.settings import AppSettings, SettingsStore
from llmbatdesk.storage import MetadataStore


def test_directory_scanning_and_duplicate_prevention(tmp_path: Path) -> None:
    root = tmp_path / "scripts"
    root.mkdir()
    script = root / "a.bat"
    script.write_text("ollama serve", encoding="utf-8")
    (root / "b.cmd").write_text("ollama run qwen", encoding="utf-8")
    records = ScriptScanner().scan([root, root], [script])
    assert len(records) == 2


def test_same_filename_different_directories(tmp_path: Path) -> None:
    for directory in ("a", "b"):
        target = tmp_path / directory
        target.mkdir()
        (target / "start.bat").write_text(f"rem {directory}\nollama serve", encoding="utf-8")
    assert len(ScriptScanner().scan([tmp_path])) == 2


def test_exclusion_patterns(tmp_path: Path) -> None:
    ignored = tmp_path / ".venv"
    ignored.mkdir()
    (ignored / "bad.bat").write_text("bad", encoding="utf-8")
    (tmp_path / "good.bat").write_text("ollama serve", encoding="utf-8")
    records = ScriptScanner([".venv"]).scan([tmp_path])
    assert [record.parsed.path.name for record in records] == ["good.bat"]


def test_rescan_after_content_change(tmp_path: Path) -> None:
    script = tmp_path / "x.bat"
    script.write_text("llama-server.exe -m x.gguf --port 8080 -c 32768", encoding="utf-8")
    before = ScriptScanner().scan([tmp_path])[0]
    script.write_text("llama-server.exe -m x.gguf --port 8080 -c 49152", encoding="utf-8")
    after = ScriptScanner().scan([tmp_path])[0]
    assert before.fingerprint.sha256 != after.fingerprint.sha256
    assert after.parsed.context_size == 49152


def test_likely_move_by_unique_hash(tmp_path: Path) -> None:
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    script = old_dir / "x.bat"
    script.write_text("ollama serve", encoding="utf-8")
    old = ScriptScanner().scan([old_dir])
    new_dir.mkdir()
    script.rename(new_dir / "x.bat")
    new = ScriptScanner().scan([new_dir])
    moves = likely_moves(old, new)
    assert len(moves) == 1


def test_metadata_persistence_tags_favorite_and_chinese(tmp_path: Path) -> None:
    db = MetadataStore(tmp_path / "data.db")
    value = Metadata(
        canonical_path="c:\\脚本\\x.bat", display_name="日常角色扮演",
        favorite=True, tags=["自然语言", "常用"], games=["Elin"], notes="中文笔记",
    )
    db.save_metadata(value)
    loaded = MetadataStore(tmp_path / "data.db").get_metadata(value.canonical_path)
    assert loaded == value


def test_metadata_path_migration(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "data.db")
    store.save_metadata(Metadata(canonical_path="old", notes="keep"))
    assert store.migrate_path("old", "new")
    assert store.get_metadata("new").notes == "keep"


def test_trust_is_content_hash_specific(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path / "data.db")
    store.trust("hash-one")
    assert store.is_trusted("hash-one")
    assert not store.is_trusted("hash-two")


def test_application_settings_roundtrip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    settings = AppSettings(roots=[r"D:\模型脚本"], editor_path="notepad.exe")
    store.save(settings)
    assert store.load() == settings

