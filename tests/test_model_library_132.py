from __future__ import annotations

import json
import sqlite3
import struct
import threading
from pathlib import Path

from PySide6.QtCore import QModelIndex

from llmbatdesk.domain.models import (
    Backend, Evidence, ParseConfidence, ParsedScript, ScriptFingerprint,
    ScriptRecord,
)
from llmbatdesk.extensions.model_library.gguf_reader import (
    METADATA_READER_VERSION, METADATA_SCHEMA_VERSION, parameter_scale,
)
from llmbatdesk.extensions.model_library.models import (
    ModelFileRole, ModelRecord, ModelStatus, ParseStatus, ReferenceRole,
    ReferenceState, ScriptReference,
)
from llmbatdesk.extensions.model_library.page import ModelLibraryPage
from llmbatdesk.extensions.model_library.presentation import (
    choose_model_size, friendly_architecture, role_from_metadata,
)
from llmbatdesk.extensions.model_library.quantization import (
    LLAMA_FTYPE_LABELS, quantization_label,
)
from llmbatdesk.extensions.model_library.qt_models import (
    ModelFilterProxy, ModelTableModel,
)
from llmbatdesk.extensions.model_library.scanner import (
    ModelScanner, _record_from_metadata,
)
from llmbatdesk.extensions.model_library.service import ModelLibraryService
from llmbatdesk.extensions.model_library.storage import ModelLibraryStore


def _string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _gguf(values: dict[str, tuple[int, object]] | None = None) -> bytes:
    values = values or {}
    result = bytearray(b"GGUF" + struct.pack("<IQQ", 3, 0, len(values)))
    for key, (kind, value) in values.items():
        result += _string(key) + struct.pack("<I", kind)
        if kind == 8:
            result += _string(str(value))
        elif kind == 4:
            result += struct.pack("<I", int(value))
        else:
            raise AssertionError(kind)
    return bytes(result)


def _model_record(
    root_id: int, path: Path, file_stat, scanned_at: str
) -> ModelRecord:
    return ModelRecord(
        model_id=None, root_id=root_id, canonical_path=str(path),
        filename=path.name, size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns, status=ModelStatus.AVAILABLE,
        parse_status=ParseStatus.PARSED, name=path.stem,
        metadata_reader_version=METADATA_READER_VERSION,
        metadata_schema_version=METADATA_SCHEMA_VERSION,
        last_parse_outcome_class="success", last_scanned_at=scanned_at,
    )


def _configured(tmp_path: Path):
    root_path = tmp_path / "models"
    root_path.mkdir()
    model_path = root_path / "model.gguf"
    model_path.write_bytes(_gguf())
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    root = store.add_root(str(root_path.resolve()), "models")
    return store, root, model_path


def _set_cache(
    store: ModelLibraryStore, path: Path, *, reader: int,
    schema: int = METADATA_SCHEMA_VERSION, outcome: str,
    parse_status: str = "error", error: str = "old error",
) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE models SET metadata_reader_version=?,
                metadata_schema_version=?, last_parse_outcome_class=?,
                parse_status=?, parse_error=? WHERE canonical_path=?
            """,
            (reader, schema, outcome, parse_status, error, str(path.resolve())),
        )


def test_stale_reader_version_forces_manual_reparse_and_clears_old_error(
    tmp_path: Path,
) -> None:
    store, root, path = _configured(tmp_path)
    scanner = ModelScanner(store, reader=_model_record)
    scanner.scan("initial", [root], threading.Event())
    store.save_user_metadata(store.models()[0].model_id, ["保留"], "用户备注")
    _set_cache(
        store, path, reader=METADATA_READER_VERSION - 1,
        outcome="safety_limit", parse_status="safety_limit",
        error="GGUF 数组元素总数超过安全上限",
    )
    calls: list[Path] = []

    def reader(root_id, value, file_stat, scanned_at):
        calls.append(value)
        return _model_record(root_id, value, file_stat, scanned_at)

    result = ModelScanner(store, reader=reader).scan(
        "upgrade", [root], threading.Event()
    )
    refreshed = store.models()[0]
    assert calls == [path.resolve()]
    assert result.parsed == 1
    assert "旧版元数据缓存" in "\n".join(result.messages)
    assert refreshed.parse_error == ""
    assert refreshed.parse_status == ParseStatus.PARSED
    assert refreshed.tags == ["保留"]
    assert refreshed.notes == "用户备注"


def test_current_success_and_permanent_failures_remain_cached(
    tmp_path: Path,
) -> None:
    for outcome in ("success", "malformed", "unsupported_version", "safety_limit"):
        case = tmp_path / outcome
        case.mkdir()
        store, root, path = _configured(case)
        store.upsert_model(_model_record(root.root_id, path, path.stat(), "now"))
        _set_cache(
            store, path, reader=METADATA_READER_VERSION, outcome=outcome,
            parse_status="malformed" if outcome != "success" else "parsed",
        )
        scanner = ModelScanner(
            store,
            reader=lambda *_args: (_ for _ in ()).throw(
                AssertionError("当前永久结果不应重读")
            ),
        )
        result = scanner.scan(outcome, [root], threading.Event())
        assert result.unchanged == 1


def test_transient_current_version_error_retries_on_next_manual_scan(
    tmp_path: Path,
) -> None:
    store, root, path = _configured(tmp_path)
    store.upsert_model(_model_record(root.root_id, path, path.stat(), "now"))
    _set_cache(
        store, path, reader=METADATA_READER_VERSION, outcome="io_error",
        parse_status="io_error", error="permission denied",
    )
    calls = 0

    def reader(root_id, value, file_stat, scanned_at):
        nonlocal calls
        calls += 1
        return _model_record(root_id, value, file_stat, scanned_at)

    result = ModelScanner(store, reader=reader).scan(
        "retry", [root], threading.Event()
    )
    assert calls == 1
    assert result.parsed == 1


def test_schema_migration_preserves_roots_tags_notes_and_references(
    tmp_path: Path,
) -> None:
    database = tmp_path / "model_library.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE scan_roots(
          id INTEGER PRIMARY KEY, canonical_path TEXT UNIQUE, display_name TEXT,
          enabled INTEGER DEFAULT 1, recursive INTEGER DEFAULT 1,
          last_scan_at TEXT, indexed_count INTEGER DEFAULT 0,
          available INTEGER DEFAULT 1, status_message TEXT DEFAULT '');
        CREATE TABLE models(
          id INTEGER PRIMARY KEY, root_id INTEGER, canonical_path TEXT UNIQUE,
          filename TEXT, size INTEGER, mtime_ns INTEGER, status TEXT,
          parse_status TEXT, parse_error TEXT DEFAULT '', name TEXT DEFAULT '',
          architecture TEXT DEFAULT '', parameter_count INTEGER,
          quantization TEXT DEFAULT '', size_label TEXT DEFAULT '',
          tokenizer_family TEXT DEFAULT '', context_length INTEGER,
          tensor_count INTEGER, metadata_json TEXT DEFAULT '{}',
          provenance_json TEXT DEFAULT '{}', tags_json TEXT DEFAULT '[]',
          notes TEXT DEFAULT '', possible_duplicate INTEGER DEFAULT 0,
          last_scanned_at TEXT);
        CREATE TABLE script_references(
          id INTEGER PRIMARY KEY, script_path TEXT, script_name TEXT,
          role TEXT, state TEXT, model_path TEXT DEFAULT '',
          candidates_json TEXT DEFAULT '[]', reason TEXT DEFAULT '');
        INSERT INTO scan_roots(id,canonical_path,display_name) VALUES(1,'C:/models','模型');
        INSERT INTO models(
          root_id,canonical_path,filename,size,mtime_ns,status,parse_status,
          tags_json,notes
        ) VALUES(1,'C:/models/a.gguf','a.gguf',1,2,'available','error','["标签"]','备注');
        INSERT INTO script_references(
          script_path,script_name,role,state,model_path
        ) VALUES('x.bat','x','primary','missing','C:/models/a.gguf');
        """
    )
    connection.commit()
    connection.close()
    store = ModelLibraryStore(database)
    record = store.models()[0]
    assert store.roots()[0].display_name == "模型"
    assert record.tags == ["标签"]
    assert record.notes == "备注"
    assert record.reference_count == 1
    assert record.metadata_reader_version == 0


def test_explicit_refresh_always_reparses_and_preserves_file(
    tmp_path: Path, monkeypatch,
) -> None:
    store, root, path = _configured(tmp_path)
    store.upsert_model(_model_record(root.root_id, path, path.stat(), "now"))
    before = path.read_bytes()
    calls = 0

    def replacement(root_id, value, file_stat, scanned_at, cancelled=None):
        nonlocal calls
        calls += 1
        return _model_record(root_id, value, file_stat, scanned_at)

    monkeypatch.setattr(
        "llmbatdesk.extensions.model_library.scanner._record_from_metadata",
        replacement,
    )
    service = ModelLibraryService(tmp_path, store=store)
    service.rescan_one(store.models()[0].model_id)
    assert calls == 1
    assert path.read_bytes() == before


def test_role_classification_precedence_and_provenance() -> None:
    assert role_from_metadata(
        general_type="", architecture="clip", filename="anything.gguf",
        tensor_count=1,
    ) == (ModelFileRole.MMPROJ, "metadata")
    assert role_from_metadata(
        general_type="", architecture="gemma4-assistant",
        filename="anything.gguf", tensor_count=1,
    )[0] == ModelFileRole.DRAFT
    assert role_from_metadata(
        general_type="", architecture="", filename="mmproj-model.gguf",
        tensor_count=0,
    ) == (ModelFileRole.MMPROJ, "filename heuristic")
    assert role_from_metadata(
        general_type="", architecture="", filename="mystery.gguf",
        tensor_count=0,
    ) == (ModelFileRole.UNKNOWN, "unknown")


def test_default_hides_auxiliary_and_toggle_restores_it() -> None:
    primary = ModelRecord(
        model_id=1, root_id=1, canonical_path="primary.gguf",
        filename="primary.gguf", size=1, mtime_ns=1,
        model_role=ModelFileRole.PRIMARY,
    )
    auxiliary = ModelRecord(
        model_id=2, root_id=1, canonical_path="mmproj.gguf",
        filename="mmproj.gguf", size=1, mtime_ns=1,
        model_role=ModelFileRole.MMPROJ,
    )
    model = ModelTableModel()
    model.set_records([primary, auxiliary])
    proxy = ModelFilterProxy()
    proxy.setSourceModel(model)
    assert proxy.rowCount() == 1
    proxy.set_filters(show_auxiliary=True)
    assert proxy.rowCount() == 2


def test_shared_auxiliary_component_many_to_many_without_duplicate_model(
    tmp_path: Path,
) -> None:
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    root_path = tmp_path / "models"
    root_path.mkdir()
    root = store.add_root(str(root_path.resolve()), "models")
    paths = [
        root_path / name
        for name in ("one.gguf", "two.gguf", "mmproj.gguf", "mtp.gguf")
    ]
    for index, path in enumerate(paths):
        path.write_bytes(b"x")
        store.upsert_model(ModelRecord(
            model_id=None, root_id=root.root_id,
            canonical_path=str(path.resolve()), filename=path.name,
            size=1, mtime_ns=path.stat().st_mtime_ns,
            metadata_role=(
                (
                    ModelFileRole.MMPROJ if index == 2
                    else ModelFileRole.DRAFT if index == 3
                    else ModelFileRole.PRIMARY
                )
            ),
            model_role=(
                (
                    ModelFileRole.MMPROJ if index == 2
                    else ModelFileRole.DRAFT if index == 3
                    else ModelFileRole.PRIMARY
                )
            ),
        ))
    refs: list[ScriptReference] = []
    for script, primary in (("a.bat", paths[0]), ("b.bat", paths[1])):
        refs.extend((
            ScriptReference(
                script, Path(script).stem, ReferenceState.EXPLICIT,
                model_path=str(primary.resolve()), role=ReferenceRole.PRIMARY,
            ),
            ScriptReference(
                script, Path(script).stem, ReferenceState.EXPLICIT,
                model_path=str(paths[2].resolve()), role=ReferenceRole.MMPROJ,
            ),
            ScriptReference(
                script, Path(script).stem, ReferenceState.EXPLICIT,
                model_path=str(paths[3].resolve()), role=ReferenceRole.DRAFT,
            ),
        ))
    store.replace_references(refs)
    assert len(store.models()) == 4
    first = store.components_for_primary(str(paths[0].resolve()))
    second = store.components_for_primary(str(paths[1].resolve()))
    assert {item.filename for item in first} == {"mmproj.gguf", "mtp.gguf"}
    assert {item.filename for item in second} == {"mmproj.gguf", "mtp.gguf"}


def test_unreferenced_auxiliary_is_classified_from_filename(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mtp-example.gguf"
    path.write_bytes(_gguf())
    record = _record_from_metadata(1, path, path.stat(), "now")
    assert record.model_role == ModelFileRole.DRAFT
    assert record.role_provenance == "filename heuristic"
    assert path.read_bytes() == _gguf()


def test_friendly_architecture_mapping_and_safe_fallback() -> None:
    assert friendly_architecture("gemma4")[0] == "Gemma 4"
    assert friendly_architecture("qwen35")[0] == "Qwen 系列"
    assert friendly_architecture("gemma4-assistant")[0] == "Gemma 4 辅助模型"
    assert friendly_architecture("clip")[0] == "视觉编码器"
    assert friendly_architecture("hunyuan-dense")[0] == "Hunyuan Dense"
    assert friendly_architecture("future-arch")[0] == "未知架构（future-arch）"


def test_nominal_size_precedence_and_moe_active_is_not_invented() -> None:
    assert choose_model_size(
        metadata_size_label="26B-A4B", model_name="x", filename="x.gguf",
        parameter_count=26_900_000_000, calculated_formatter=parameter_scale,
    ) == ("26B-A4B", "metadata: general.size_label")
    assert choose_model_size(
        metadata_size_label="", model_name="model",
        filename="model-27B-Q4_K_M.gguf",
        parameter_count=26_900_000_000, calculated_formatter=parameter_scale,
    ) == ("27B", "filename heuristic")
    assert choose_model_size(
        metadata_size_label="", model_name="model", filename="model.gguf",
        parameter_count=26_900_000_000, calculated_formatter=parameter_scale,
    ) == ("26.9B", "calculated")
    assert choose_model_size(
        metadata_size_label="", model_name="Gemma-26B-A4B",
        filename="model.gguf", parameter_count=26_000_000_000,
        calculated_formatter=parameter_scale,
    ) == ("26B-A4B", "metadata name heuristic")
    record = ModelRecord(
        model_id=1, root_id=1, canonical_path="mtp.gguf",
        filename="mtp.gguf", size=1, mtime_ns=1,
        parameter_count=419_700_000, active_parameter_count=None,
        model_role=ModelFileRole.DRAFT,
    )
    assert record.parameter_count == 419_700_000
    assert record.active_parameter_count is None


def test_exhaustive_current_llama_ftype_mapping_and_unknown_future() -> None:
    assert set(LLAMA_FTYPE_LABELS) == set(range(42))
    required = {
        0: "F32", 1: "F16", 7: "Q8_0", 12: "Q3_K_M",
        15: "Q4_K_M", 18: "Q6_K", 32: "BF16",
        36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4 MoE",
        39: "NVFP4", 40: "Q1_0", 41: "Q2_0",
    }
    for value, expected in required.items():
        assert quantization_label(value) == expected
    assert quantization_label(999) == "未知（file_type=999）"


def test_component_area_and_auxiliary_toggle_ui(qtbot, tmp_path: Path) -> None:
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    root_path = tmp_path / "models"
    root_path.mkdir()
    root = store.add_root(str(root_path.resolve()), "models")
    primary, mmproj = root_path / "main.gguf", root_path / "mmproj.gguf"
    for path, role in (
        (primary, ModelFileRole.PRIMARY),
        (mmproj, ModelFileRole.MMPROJ),
    ):
        path.write_bytes(b"x")
        store.upsert_model(ModelRecord(
            model_id=None, root_id=root.root_id,
            canonical_path=str(path.resolve()), filename=path.name,
            size=1, mtime_ns=path.stat().st_mtime_ns,
            model_role=role, metadata_role=role,
        ))
    script = tmp_path / "launch.bat"
    script.write_bytes(b"")
    parsed = ParsedScript(
        path=script, backend=Backend.LLAMA_CPP,
        confidence=ParseConfidence.FULL, model_path=str(primary),
        evidence=[
            Evidence(
                line_number=1, source_line="",
                normalized_token=f"--model={primary}",
                field="model_path", reason="",
            ),
            Evidence(
                line_number=1, source_line="",
                normalized_token=f"--mmproj={mmproj}",
                field="mmproj_path", reason="",
            ),
        ],
    )
    record = ScriptRecord(
        fingerprint=ScriptFingerprint(
            canonical_path=str(script), size=0, mtime_ns=0, sha256="a" * 64
        ),
        parsed=parsed,
    )
    service = ModelLibraryService(tmp_path, store=store)
    page = ModelLibraryPage(tmp_path, [record], service=service)
    qtbot.addWidget(page)
    assert page.proxy_model.rowCount() == 1
    page.model_table.selectRow(0)
    qtbot.waitUntil(lambda: page.component_model.rowCount() == 1)
    assert page.component_model.index(0, 0).data() == "mmproj.gguf"
    page.show_auxiliary.setChecked(True)
    assert page.proxy_model.rowCount() == 2
    page.shutdown()
