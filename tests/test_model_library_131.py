from __future__ import annotations

import json
import struct
import threading
import tracemalloc
from pathlib import Path

import pytest
from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QHeaderView

from llmbatdesk.domain.models import ScriptFingerprint, ScriptRecord
from llmbatdesk.extensions.model_library.gguf_reader import (
    GgufCancelled, GgufFormatError, GgufSafetyLimitError,
    MAX_METADATA_BYTES, read_gguf_metadata,
)
from llmbatdesk.extensions.model_library.models import (
    ModelRecord, ReferenceRole, ReferenceState,
)
from llmbatdesk.extensions.model_library.page import ModelLibraryPage
from llmbatdesk.extensions.model_library.quantization import quantization_label
from llmbatdesk.extensions.model_library.references import references_from_records
from llmbatdesk.extensions.model_library.scanner import _record_from_metadata
from llmbatdesk.extensions.model_library.service import ModelLibraryService
from llmbatdesk.extensions.model_library.storage import ModelLibraryStore
from llmbatdesk.parsing import parse_script_bytes
from llmbatdesk.settings import AppSettings


def _string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _array_file(
    path: Path, *, key: str, element_type: int, count: int, data: bytes
) -> None:
    path.write_bytes(
        b"GGUF" + struct.pack("<IQQ", 3, 0, 1)
        + _string(key) + struct.pack("<IIQ", 9, element_type, count) + data
    )


def _record(tmp_path: Path, name: str, command: str) -> ScriptRecord:
    path = tmp_path / f"{name}.bat"
    raw = command.encode("utf-8")
    path.write_bytes(raw)
    parsed = parse_script_bytes(raw, path)
    return ScriptRecord(
        fingerprint=ScriptFingerprint(
            canonical_path=str(path.resolve()), size=len(raw),
            mtime_ns=path.stat().st_mtime_ns, sha256=name * 8,
        ),
        parsed=parsed,
    )


def test_large_fixed_array_over_one_million_is_skipped_with_bounded_memory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large.gguf"
    count = 1_100_001
    _array_file(
        path, key="tokenizer.ggml.token_type", element_type=0,
        count=count, data=b"\0" * count,
    )
    tracemalloc.start()
    metadata = read_gguf_metadata(path)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert metadata.tensor_count == 0
    assert peak < 4 * 1024 * 1024


def test_large_string_array_is_streamed_without_retention(tmp_path: Path) -> None:
    path = tmp_path / "tokens.gguf"
    count = 1_000_001
    _array_file(
        path, key="tokenizer.ggml.tokens", element_type=8,
        count=count, data=struct.pack("<Q", 0) * count,
    )
    metadata = read_gguf_metadata(path)
    assert "tokenizer.ggml.tokens" not in metadata.fields


def test_large_fixed_width_array_is_skipped_safely(tmp_path: Path) -> None:
    path = tmp_path / "scores.gguf"
    count = 1_050_000
    _array_file(
        path, key="tokenizer.ggml.scores", element_type=6,
        count=count, data=b"\0" * (count * 4),
    )
    assert read_gguf_metadata(path).bytes_read == path.stat().st_size


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            struct.pack("<Q", 2 * 1024 * 1024),
            "字符串长度",
        ),
        (
            struct.pack("<Q", 3) + b"x",
            "截断",
        ),
    ],
)
def test_malformed_or_truncated_string_arrays(
    tmp_path: Path, payload: bytes, expected: str
) -> None:
    path = tmp_path / "bad.gguf"
    _array_file(
        path, key="tokenizer.ggml.tokens", element_type=8,
        count=1, data=payload,
    )
    with pytest.raises(GgufFormatError, match=expected):
        read_gguf_metadata(path)


def test_excessive_metadata_extent_and_integer_count_are_rejected(
    tmp_path: Path,
) -> None:
    path = tmp_path / "extent.gguf"
    header = (
        b"GGUF" + struct.pack("<IQQ", 3, 0, 1)
        + _string("ignored") + struct.pack("<IIQ", 9, 10, 8_388_608)
    )
    with path.open("wb") as handle:
        handle.write(header)
        handle.seek((8_388_608 * 8) - 1, 1)
        handle.write(b"\0")
    with pytest.raises(GgufSafetyLimitError, match="读取上限"):
        read_gguf_metadata(path)

    overflow = tmp_path / "overflow.gguf"
    _array_file(
        overflow, key="ignored", element_type=0,
        count=(1 << 64) - 1, data=b"",
    )
    with pytest.raises(GgufSafetyLimitError, match="元素数量"):
        read_gguf_metadata(overflow)


def test_excessive_nesting_and_cancellation_in_large_section(
    tmp_path: Path,
) -> None:
    nested = struct.pack("<IQ", 9, 1) * 5 + struct.pack("<IQ", 0, 0)
    path = tmp_path / "nested.gguf"
    _array_file(path, key="nested", element_type=9, count=1, data=nested)
    with pytest.raises(GgufSafetyLimitError, match="嵌套"):
        read_gguf_metadata(path)

    cancel_path = tmp_path / "cancel.gguf"
    _array_file(
        cancel_path, key="tokenizer.ggml.tokens", element_type=8,
        count=1_000_001, data=struct.pack("<Q", 0) * 1_000_001,
    )
    class _CancelDuringArray:
        def __init__(self) -> None:
            self.calls = 0

        def is_set(self) -> bool:
            self.calls += 1
            return self.calls >= 3

    cancelled = _CancelDuringArray()
    with pytest.raises(GgufCancelled):
        read_gguf_metadata(cancel_path, cancelled=cancelled)


@pytest.mark.parametrize(
    ("value", "label"),
    [(7, "Q8_0"), (15, "Q4_K_M"), (18, "Q6_K"), (999, "未知（file_type=999）")],
)
def test_quantization_mapping(value: int, label: str) -> None:
    assert quantization_label(value) == label


def test_metadata_provenance_and_auxiliary_unknown_reason(tmp_path: Path) -> None:
    full = tmp_path / "full.gguf"
    full.write_bytes(
        b"GGUF" + struct.pack("<IQQ", 3, 1, 3)
        + _string("general.name") + struct.pack("<I", 8) + _string("Model")
        + _string("general.architecture") + struct.pack("<I", 8) + _string("llama")
        + _string("general.file_type") + struct.pack("<II", 4, 15)
        + _string("w") + struct.pack("<IQQIQ", 2, 10, 20, 0, 0)
    )
    record = _record_from_metadata(1, full, full.stat(), "now")
    provenance = json.loads(record.provenance_json)
    assert record.quantization == "Q4_K_M"
    assert record.parameter_count == 200
    assert provenance["display.architecture"] == "metadata"
    assert provenance["display.parameter_count"] == "calculated"

    auxiliary = tmp_path / "mmproj.gguf"
    auxiliary.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 0))
    aux = _record_from_metadata(1, auxiliary, auxiliary.stat(), "now")
    why = json.loads(aux.provenance_json)["display.architecture"]
    assert "辅助或最小 GGUF" in why


def test_role_aware_primary_and_auxiliary_references(tmp_path: Path) -> None:
    primary = tmp_path / "main.gguf"
    mmproj = tmp_path / "mmproj.gguf"
    draft = tmp_path / "draft.gguf"
    adapter = tmp_path / "adapter.gguf"
    for path in (primary, mmproj, draft, adapter):
        path.write_bytes(b"x")
    refs = references_from_records([_record(
        tmp_path, "roles",
        f'llama-server.exe -m "{primary}" --mmproj "{mmproj}" '
        f'--draft-model "{draft}" --lora "{adapter}"',
    )])
    assert {(item.role, item.state) for item in refs} == {
        (ReferenceRole.PRIMARY, ReferenceState.EXPLICIT),
        (ReferenceRole.MMPROJ, ReferenceState.EXPLICIT),
        (ReferenceRole.DRAFT, ReferenceState.EXPLICIT),
        (ReferenceRole.ADAPTER, ReferenceState.EXPLICIT),
    }


def test_reference_ambiguity_dedup_dynamic_and_relative_paths(
    tmp_path: Path,
) -> None:
    same = references_from_records([_record(
        tmp_path, "same",
        "llama-server.exe -m main.gguf --model main.gguf",
    )])
    assert len(same) == 1
    assert same[0].role == ReferenceRole.PRIMARY
    assert same[0].state == ReferenceState.MISSING

    ambiguous = references_from_records([_record(
        tmp_path, "ambiguous",
        "llama-server.exe -m one.gguf --model two.gguf",
    )])
    assert ambiguous[0].role == ReferenceRole.AMBIGUOUS
    assert ambiguous[0].model_path == ""
    assert len(ambiguous[0].candidates) == 2

    dynamic = references_from_records([_record(
        tmp_path, "dynamic",
        "set /p MODEL=\nllama-server.exe -m %MODEL%",
    )])
    assert dynamic[0].state == ReferenceState.DYNAMIC
    assert dynamic[0].model_path == ""


def test_missing_primary_and_mmproj_are_separate_and_counted(
    tmp_path: Path,
) -> None:
    root_path = tmp_path / "models"
    root_path.mkdir()
    primary = root_path / "main.gguf"
    mmproj = root_path / "mmproj.gguf"
    primary.write_bytes(b"x")
    mmproj.write_bytes(b"x")
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    root = store.add_root(str(root_path.resolve()), "models")
    for path in (primary, mmproj):
        store.upsert_model(ModelRecord(
            model_id=None, root_id=root.root_id,
            canonical_path=str(path.resolve()), filename=path.name,
            size=1, mtime_ns=path.stat().st_mtime_ns,
        ))
    records = [_record(
        tmp_path, "linked",
        f'llama-server.exe -m "{primary}" --mmproj "{mmproj}"',
    )]
    store.replace_references(references_from_records(records))
    indexed = {item.filename: item for item in store.models()}
    assert indexed["main.gguf"].primary_reference_count == 1
    assert indexed["main.gguf"].auxiliary_reference_count == 0
    assert indexed["mmproj.gguf"].primary_reference_count == 0
    assert indexed["mmproj.gguf"].auxiliary_reference_count == 1
    assert store.references_for_model(str(mmproj.resolve()))[0].role == ReferenceRole.MMPROJ


def test_selected_model_reference_table_shows_role_and_state(
    qtbot, tmp_path: Path,
) -> None:
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    model = model_dir / "main.gguf"
    model.write_bytes(b"x")
    record = _record(
        tmp_path, "launcher", f'llama-server.exe -m "{model}"'
    )
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    root = store.add_root(str(model_dir.resolve()), "models")
    store.upsert_model(ModelRecord(
        model_id=None, root_id=root.root_id,
        canonical_path=str(model.resolve()), filename=model.name,
        size=1, mtime_ns=model.stat().st_mtime_ns,
    ))
    service = ModelLibraryService(tmp_path, store=store)
    page = ModelLibraryPage(tmp_path, [record], service=service)
    qtbot.addWidget(page)
    page.model_table.selectRow(0)
    qtbot.waitUntil(lambda: page.reference_model.rowCount() == 1)
    assert page.reference_model.index(0, 1).data() == "主模型"
    assert page.reference_model.index(0, 2).data() == "明确引用"
    page.shutdown()


def test_missing_auxiliary_keeps_role_and_does_not_make_primary_ambiguous(
    tmp_path: Path,
) -> None:
    refs = references_from_records([_record(
        tmp_path, "missing_roles",
        "llama-server.exe -m main.gguf --mmproj missing-mmproj.gguf",
    )])
    assert {(item.role, item.state) for item in refs} == {
        (ReferenceRole.PRIMARY, ReferenceState.MISSING),
        (ReferenceRole.MMPROJ, ReferenceState.MISSING),
    }


def test_legacy_reference_schema_migrates_without_core_database_effect(
    tmp_path: Path,
) -> None:
    database = tmp_path / "model_library.sqlite"
    connection = __import__("sqlite3").connect(database)
    connection.execute(
        """
        CREATE TABLE script_references(
            script_path TEXT PRIMARY KEY, script_name TEXT NOT NULL,
            state TEXT NOT NULL, model_path TEXT NOT NULL DEFAULT '',
            candidates_json TEXT NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        "INSERT INTO script_references VALUES(?,?,?,?,?,?)",
        ("x.bat", "x", "missing", "x.gguf", "[]", "missing"),
    )
    connection.commit()
    connection.close()
    store = ModelLibraryStore(database)
    rows = store.problem_references()
    assert rows[0].role == ReferenceRole.PRIMARY
    assert rows[0].state == ReferenceState.MISSING


def test_root_add_updates_visible_model_selects_and_does_not_scan(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "中文 模型"
    root.mkdir()
    service = ModelLibraryService(tmp_path / "data")
    page = ModelLibraryPage(tmp_path / "data", [], service=service)
    qtbot.addWidget(page)
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *_args: str(root)
    )
    scans: list[object] = []
    monkeypatch.setattr(page, "_start_scan", lambda *_args: scans.append(True))
    inserted: list[tuple[int, int]] = []
    page.root_model.rowsInserted.connect(
        lambda _parent, first, last: inserted.append((first, last))
    )
    page.add_root()
    assert page.root_model.rowCount() == 1
    assert inserted == [(0, 0)]
    assert page.root_table.selectionModel().selectedRows()[0].row() == 0
    assert page.scan_status.text() == "扫描目录已添加"
    assert scans == []
    page.shutdown()


def test_persisted_duplicate_remove_and_root_flags(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "模型"
    root.mkdir()
    service = ModelLibraryService(tmp_path / "data")
    persisted = service.add_root(root)
    page = ModelLibraryPage(tmp_path / "data", [], service=service)
    qtbot.addWidget(page)
    assert page.root_model.rowCount() == 1
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *_args: str(root)
    )
    page.add_root()
    assert page.root_model.rowCount() == 1
    assert page.scan_status.text() == "该扫描目录已经存在"

    enabled = page.root_model.index(0, 1)
    recursive = page.root_model.index(0, 2)
    assert page.root_model.setData(
        enabled, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole
    )
    assert page.root_model.setData(
        recursive, Qt.CheckState.Unchecked, Qt.ItemDataRole.CheckStateRole
    )
    loaded = service.store.root(persisted.root_id)
    assert loaded and not loaded.enabled and not loaded.recursive

    removed: list[tuple[int, int]] = []
    page.root_model.rowsRemoved.connect(
        lambda _parent, first, last: removed.append((first, last))
    )
    page.root_table.selectRow(0)
    page.remove_selected_root()
    assert page.root_model.rowCount() == 0
    assert removed == [(0, 0)]
    page.shutdown()


def test_root_add_storage_failure_is_localized(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "models"
    root.mkdir()
    service = ModelLibraryService(tmp_path / "data")
    page = ModelLibraryPage(tmp_path / "data", [], service=service)
    qtbot.addWidget(page)
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory", lambda *_args: str(root)
    )
    monkeypatch.setattr(
        service, "add_root_with_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("无法保存扫描目录")
        ),
    )
    page.add_root()
    assert page.root_model.rowCount() == 0
    assert page.scan_status.text() == "无法保存扫描目录"
    page.shutdown()


def test_column_headers_are_interactive_resizable_and_persistent(
    qtbot, tmp_path: Path,
) -> None:
    widths = {
        "scan_roots": [301, 72, 73, 302],
        "models": [201, 101, 102, 103, 104, 105, 206],
        "references": [211, 111, 112],
    }
    page = ModelLibraryPage(tmp_path, [], column_widths=widths)
    qtbot.addWidget(page)
    for table in (page.root_table, page.model_table, page.references):
        header = table.horizontalHeader()
        assert not header.stretchLastSection()
        assert all(
            header.sectionResizeMode(column)
            == QHeaderView.ResizeMode.Interactive
            for column in range(table.model().columnCount())
        )
    assert page.model_table.columnWidth(6) == 206
    page.model_table.setColumnWidth(0, 333)
    stored = page.column_widths()
    assert stored["models"][0] == 333
    assert page.findChildren(QTimer) == []
    page._reset_column_widths(page.model_table, "models")
    assert page.model_table.columnWidth(0) == 240
    page.shutdown()

    settings = AppSettings(
        model_library_column_widths={"models": ["bad"], "other": [1]}
    )
    assert settings.model_library_column_widths.get("models") == []
