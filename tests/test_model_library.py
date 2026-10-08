from __future__ import annotations

import json
import os
import sqlite3
import struct
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QAbstractTableModel, QTimer

from llmbatdesk.domain.models import (
    Backend, Evidence, ParsedScript, ParseConfidence, ScriptFingerprint,
    ScriptRecord,
)
from llmbatdesk.extensions.model_library.gguf_reader import (
    GgufFormatError, MAX_STRING_BYTES, read_gguf_metadata,
)
from llmbatdesk.extensions.model_library.models import (
    GgufMetadata, ModelRecord, ModelStatus, ParseStatus, ReferenceState, ScanSummary,
    ScanSuppressedError,
)
from llmbatdesk.extensions.model_library.page import ModelLibraryPage
from llmbatdesk.extensions.model_library.qt_models import (
    ModelFilterProxy, ModelTableModel,
)
from llmbatdesk.extensions.model_library.references import references_from_records
from llmbatdesk.extensions.model_library.scanner import (
    ModelScanner, canonical_scan_root, iter_gguf_files,
)
from llmbatdesk.extensions.model_library.service import ModelLibraryService
from llmbatdesk.extensions.model_library.storage import ModelLibraryStore
from llmbatdesk.qt.dialogs import SettingsDialog
from llmbatdesk.settings import AppSettings, SettingsStore


def _string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def synthetic_gguf(
    values: dict[str, tuple[int, object]] | None = None,
    *,
    tensors: list[tuple[str, tuple[int, ...]]] | None = None,
    payload_size: int = 0,
) -> bytes:
    values = values or {}
    tensors = tensors or []
    result = bytearray(
        b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(values))
    )
    scalar_formats = {
        4: "<I", 10: "<Q", 11: "<q", 12: "<d", 7: "<?",
    }
    for key, (value_type, value) in values.items():
        result += _string(key)
        result += struct.pack("<I", value_type)
        if value_type == 8:
            result += _string(str(value))
        else:
            result += struct.pack(scalar_formats[value_type], value)
    for name, dimensions in tensors:
        result += _string(name)
        result += struct.pack("<I", len(dimensions))
        result += struct.pack("<" + "Q" * len(dimensions), *dimensions)
        result += struct.pack("<IQ", 0, 0)
    result += b"\xA5" * payload_size
    return bytes(result)


def write_model(path: Path, *, name: str = "测试模型") -> bytes:
    data = synthetic_gguf(
        {
            "general.name": (8, name),
            "general.architecture": (8, "llama"),
            "general.file_type": (4, 7),
            "general.quantization_version": (4, 2),
            "general.size_label": (8, "3B"),
            "tokenizer.ggml.model": (8, "llama"),
            "llama.context_length": (10, 8192),
        },
        tensors=[("blk.0.weight", (32, 64)), ("output", (64, 100))],
        payload_size=512 * 1024,
    )
    path.write_bytes(data)
    return data


def model_record(root_id: int, path: Path, file_stat, scanned_at: str) -> ModelRecord:
    return ModelRecord(
        model_id=None, root_id=root_id, canonical_path=str(path),
        filename=path.name, size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns, status=ModelStatus.AVAILABLE,
        parse_status=ParseStatus.PARSED, name=path.stem,
        architecture="llama", parameter_count=123,
        quantization="Q4_K_M", last_scanned_at=scanned_at,
    )


def script_record(
    tmp_path: Path, name: str, *, model_path: str | None,
    confidence: ParseConfidence = ParseConfidence.FULL,
    dynamic: bool = False, evidence: list[str] | None = None,
    backend: Backend = Backend.LLAMA_CPP,
) -> ScriptRecord:
    script = tmp_path / f"{name}.bat"
    script.write_text("@echo off\n", encoding="utf-8")
    parsed = ParsedScript(
        path=script, backend=backend, confidence=confidence,
        model_path=model_path, model_name=(None if model_path else name),
        dynamic_reasons=(["运行时变量"] if dynamic else []),
        evidence=[
            Evidence(
                line_number=index + 1, source_line=value,
                normalized_token=value, field="model_path", reason="测试",
            )
            for index, value in enumerate(evidence or [])
        ],
    )
    return ScriptRecord(
        fingerprint=ScriptFingerprint(
            canonical_path=str(script), size=0, mtime_ns=0, sha256="x" * 64
        ),
        parsed=parsed,
    )


def test_bounded_reader_extracts_supported_metadata_without_tensor_payload(tmp_path: Path) -> None:
    path = tmp_path / "中文 模型 Q4_K_M.gguf"
    data = write_model(path)
    metadata = read_gguf_metadata(path)
    assert metadata.value("general.name") == "测试模型"
    assert metadata.value("general.architecture") == "llama"
    assert metadata.value("llama.context_length") == 8192
    assert metadata.tensor_count == 2
    assert metadata.parameter_count == 32 * 64 + 64 * 100
    assert metadata.bytes_read < len(data) - 500_000


@pytest.mark.parametrize(
    "data, message",
    [
        (b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + struct.pack("<Q", MAX_STRING_BYTES + 1), "字符串长度"),
        (b"GGUF" + struct.pack("<IQQ", 3, 200_001, 0), "tensor 数量"),
        (b"GGUF" + struct.pack("<IQQ", 3, 0, 100_001), "条目数量"),
        (b"GGUF" + struct.pack("<IQQ", 1, 0, 0), "不支持"),
        (b"GGUF\x03\x00", "截断"),
    ],
)
def test_bounded_reader_rejects_malformed_or_unreasonable_data(
    tmp_path: Path, data: bytes, message: str
) -> None:
    path = tmp_path / "bad.gguf"
    path.write_bytes(data)
    with pytest.raises(GgufFormatError, match=message):
        read_gguf_metadata(path)


def test_corrupt_model_does_not_prevent_following_model_from_indexing(tmp_path: Path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    (root / "bad.gguf").write_bytes(b"not gguf")
    write_model(root / "good.gguf")
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    configured = store.add_root(str(root.resolve()), "models")
    result = ModelScanner(store).scan(
        "mixed", [configured], threading.Event()
    )
    records = {item.filename: item for item in store.models()}
    assert result.failed == 1
    assert result.parsed == 1
    assert records["bad.gguf"].status == ModelStatus.ERROR
    assert records["good.gguf"].status == ModelStatus.AVAILABLE


def test_incremental_scan_does_not_reopen_unchanged_file_and_reparses_change(tmp_path: Path) -> None:
    root = tmp_path / "模型"
    root.mkdir()
    model = root / "a.gguf"
    model.write_bytes(b"first")
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    configured = store.add_root(str(root.resolve()), "模型")
    calls: list[str] = []

    def reader(root_id, path, file_stat, scanned_at):
        calls.append(str(path))
        return model_record(root_id, path, file_stat, scanned_at)

    scanner = ModelScanner(store, reader=reader)
    scanner.scan("one", [configured], threading.Event())
    scanner.scan("two", [configured], threading.Event())
    assert calls == [str(model.resolve())]
    model.write_bytes(b"second and changed")
    scanner.scan("three", [configured], threading.Event())
    assert calls == [str(model.resolve()), str(model.resolve())]


def test_scan_root_flags_persist_without_triggering_scan(tmp_path: Path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    configured = store.add_root(str(root.resolve()), "本地模型", recursive=True)
    store.update_root(configured.root_id, enabled=False, recursive=False)
    loaded = store.roots()[0]
    assert not loaded.enabled
    assert not loaded.recursive
    assert loaded.last_scan_at is None


def test_missing_file_retains_tags_and_notes(tmp_path: Path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    path = root / "tagged.gguf"
    path.write_bytes(b"x")
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    configured = store.add_root(str(root.resolve()), "models")
    scanner = ModelScanner(store, reader=model_record)
    scanner.scan("one", [configured], threading.Event())
    record = store.models()[0]
    store.save_user_metadata(record.model_id, ["Elin", "文风好"], "中文备注")
    path.unlink()
    scanner.scan("two", [configured], threading.Event())
    retained = store.models()[0]
    assert retained.status == ModelStatus.MISSING
    assert retained.tags == ["Elin", "文风好"]
    assert retained.notes == "中文备注"


def test_scanner_preserves_model_bytes_and_never_hashes_complete_file(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "models"
    root.mkdir()
    path = root / "safe.gguf"
    original = write_model(path)
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    configured = store.add_root(str(root.resolve()), "models")
    monkeypatch.setattr(
        "hashlib.sha256",
        lambda *_args, **_kwargs: pytest.fail("完整文件哈希不应被调用"),
    )
    ModelScanner(store).scan("scan", [configured], threading.Event())
    assert path.read_bytes() == original


def test_replaced_file_during_metadata_read_is_recorded_as_error(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "models"
    root.mkdir()
    path = root / "changing.gguf"
    write_model(path)
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    configured = store.add_root(str(root.resolve()), "models")

    def changing_reader(value: Path):
        value.write_bytes(value.read_bytes() + b"changed")
        return GgufMetadata(version=3, tensor_count=0)

    monkeypatch.setattr(
        "llmbatdesk.extensions.model_library.scanner.read_gguf_metadata",
        changing_reader,
    )
    result = ModelScanner(store).scan(
        "changed", [configured], threading.Event()
    )
    assert result.failed == 1
    assert "读取期间文件发生变化" in store.models()[0].parse_error


def test_scan_cancellation_is_cooperative_and_does_not_mark_unseen_missing(tmp_path: Path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    for index in range(4):
        (root / f"{index}.gguf").write_bytes(b"x")
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    configured = store.add_root(str(root.resolve()), "models")
    cancelled = threading.Event()
    calls = 0

    def reader(root_id, path, file_stat, scanned_at):
        nonlocal calls
        calls += 1
        cancelled.set()
        return model_record(root_id, path, file_stat, scanned_at)

    result = ModelScanner(store, reader=reader).scan(
        "cancel", [configured], cancelled
    )
    assert result.cancelled
    assert calls == 1
    assert store.models()[0].status == ModelStatus.AVAILABLE


def test_reparse_point_root_is_refused_and_child_symlink_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private.gguf").write_bytes(b"x")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前 Windows 配置不允许创建符号链接")
    assert list(iter_gguf_files(root.resolve(), recursive=True, cancelled=threading.Event())) == []
    with pytest.raises(ValueError, match="重解析点"):
        canonical_scan_root(link)


def test_filesystem_root_is_refused() -> None:
    anchor = Path(Path.cwd().anchor)
    with pytest.raises(ValueError, match="整个驱动器"):
        canonical_scan_root(anchor)


def test_inaccessible_or_deleted_root_does_not_block_other_roots(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "a.gguf").write_bytes(b"x")
    missing = tmp_path / "missing"
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    good_root = store.add_root(str(good.resolve()), "good")
    missing_root = store.add_root(str(missing), "missing")
    result = ModelScanner(store, reader=model_record).scan(
        "roots", [missing_root, good_root], threading.Event()
    )
    roots = {item.display_name: item for item in store.roots()}
    assert not roots["missing"].available
    assert roots["good"].available
    assert result.parsed == 1


def test_model_library_corruption_is_quarantined_without_touching_core_db(tmp_path: Path) -> None:
    core = tmp_path / "llmbatdesk.db"
    core.write_bytes(b"core-sentinel")
    library = tmp_path / "model_library.sqlite"
    library.write_bytes(b"not sqlite")
    store = ModelLibraryStore(library)
    assert store.roots() == []
    assert core.read_bytes() == b"core-sentinel"
    assert list(tmp_path.glob("model_library.sqlite.corrupt-*"))


def test_possible_duplicates_are_heuristic_and_clearly_marked(tmp_path: Path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    configured = store.add_root(str(root.resolve()), "models")
    for name in ("copy-a.gguf", "copy-b.gguf"):
        record = ModelRecord(
            model_id=None, root_id=configured.root_id,
            canonical_path=str((root / name).resolve()), filename=name,
            size=1234, mtime_ns=1, name="Same Model",
            architecture="llama", parameter_count=3_000_000_000,
            quantization="Q4_K_M",
        )
        store.upsert_model(record)
    store.complete_root_scan(
        configured.root_id, available=True, message="", scanned_at="now"
    )
    assert all(item.possible_duplicate for item in store.models())


def test_temporary_database_lock_is_not_treated_as_corruption(tmp_path: Path) -> None:
    database = tmp_path / "model_library.sqlite"
    store = ModelLibraryStore(database)
    locker = sqlite3.connect(database, check_same_thread=False)
    locker.execute("BEGIN EXCLUSIVE")

    def release() -> None:
        time.sleep(0.1)
        locker.rollback()
        locker.close()

    thread = threading.Thread(target=release)
    thread.start()
    assert store.roots() == []
    thread.join()
    assert not list(tmp_path.glob("model_library.sqlite.corrupt-*"))


def test_reference_states_reuse_existing_parser_results(tmp_path: Path) -> None:
    exists = tmp_path / "exists.gguf"
    exists.write_bytes(b"x")
    records = [
        script_record(tmp_path, "explicit", model_path=str(exists)),
        script_record(tmp_path, "missing", model_path=str(tmp_path / "missing.gguf")),
        script_record(tmp_path, "dynamic", model_path="%MODEL%.gguf", dynamic=True),
        script_record(
            tmp_path, "multiple", model_path=None,
            evidence=["a.gguf", "b.gguf"],
        ),
        script_record(
            tmp_path, "ollama", model_path=None, backend=Backend.OLLAMA
        ),
        script_record(
            tmp_path, "unknown", model_path=None,
            confidence=ParseConfidence.UNKNOWN,
        ),
    ]
    result = {item.script_name: item for item in references_from_records(records)}
    assert result["explicit"].state == ReferenceState.EXPLICIT
    assert result["missing"].state == ReferenceState.MISSING
    assert result["dynamic"].state == ReferenceState.DYNAMIC
    assert result["multiple"].state == ReferenceState.MULTIPLE
    assert result["ollama"].state == ReferenceState.NO_LOCAL_GGUF
    assert result["unknown"].state == ReferenceState.UNPARSED


def test_reference_sync_never_modifies_original_scripts(tmp_path: Path) -> None:
    record = script_record(
        tmp_path, "readonly", model_path=str(tmp_path / "model.gguf")
    )
    before = record.parsed.path.read_bytes()
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    store.replace_references(references_from_records([record]))
    assert record.parsed.path.read_bytes() == before


def test_reference_sync_does_not_enumerate_model_roots(tmp_path: Path, monkeypatch) -> None:
    service = ModelLibraryService(tmp_path)
    monkeypatch.setattr(
        os, "scandir", lambda *_args, **_kwargs: pytest.fail("不应枚举目录")
    )
    service.sync_references(
        [script_record(tmp_path, "missing", model_path="missing.gguf")]
    )
    assert service.store.problem_references()[0].state == ReferenceState.MISSING


def test_explicit_script_reference_updates_model_summary_without_rescan(
    tmp_path: Path
) -> None:
    model_path = tmp_path / "models" / "linked.gguf"
    model_path.parent.mkdir()
    model_path.write_bytes(b"x")
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    root = store.add_root(str(model_path.parent.resolve()), "models")
    store.upsert_model(
        ModelRecord(
            model_id=None, root_id=root.root_id,
            canonical_path=str(model_path.resolve()), filename=model_path.name,
            size=1, mtime_ns=model_path.stat().st_mtime_ns,
        )
    )
    store.replace_references(
        references_from_records(
            [script_record(tmp_path, "launcher", model_path=str(model_path))]
        )
    )
    linked = store.models()[0]
    assert linked.reference_count == 1
    assert linked.related_scripts == ["launcher"]


@pytest.mark.parametrize("active", [True])
def test_service_running_suppresses_scan_by_default(tmp_path: Path, active: bool) -> None:
    service = ModelLibraryService(tmp_path)
    service.set_service_active(active)
    with pytest.raises(ScanSuppressedError, match="模型运行期间"):
        service.begin_scan()
    assert not service.scanning


def test_running_scan_advanced_option_still_requires_explicit_begin(tmp_path: Path) -> None:
    service = ModelLibraryService(tmp_path, allow_running_scan=True)
    service.set_service_active(True)
    assert not service.scanning
    job = service.begin_scan()
    assert service.scanning
    service.cancel_scan()
    result = service.run_scan(job)
    assert result.cancelled


def test_active_scan_is_cancelled_on_service_transition_and_never_auto_resumes(tmp_path: Path) -> None:
    service = ModelLibraryService(tmp_path)
    job = service.begin_scan()
    assert service.set_service_active(True)
    assert service._cancel.is_set()
    result = service.run_scan(job)
    assert result.cancelled
    assert not service.scanning
    service.set_service_active(False)
    assert not service.scanning


def test_stale_scan_job_is_rejected(tmp_path: Path) -> None:
    service = ModelLibraryService(tmp_path)
    old = service.begin_scan()
    service.cancel_scan()
    service.run_scan(old)
    new = service.begin_scan()
    assert not service.accepts_result(old)
    assert service.accepts_result(new)
    service.cancel_scan()
    service.run_scan(new)


def test_settings_defaults_and_feature_controls(qtbot, tmp_path: Path) -> None:
    settings = AppSettings()
    assert not settings.model_library_enabled
    assert not settings.model_library_allow_running_scan
    dialog = SettingsDialog(settings, tmp_path)
    qtbot.addWidget(dialog)
    assert not dialog.model_library_enabled.isChecked()
    assert not dialog.model_library_allow_running_scan.isEnabled()
    dialog.model_library_enabled.setChecked(True)
    assert dialog.model_library_allow_running_scan.isEnabled()
    dialog.model_library_allow_running_scan.setChecked(True)
    dialog._save()
    assert dialog.settings.model_library_enabled
    assert dialog.settings.model_library_allow_running_scan


def test_malformed_extension_settings_do_not_discard_core_settings(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "roots": [r"D:\脚本"],
                "theme": "light",
                "model_library_enabled": "not-a-boolean",
                "model_library_allow_running_scan": {"bad": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = SettingsStore(tmp_path).load()
    assert settings.roots == [r"D:\脚本"]
    assert settings.theme == "light"
    assert not settings.model_library_enabled
    assert not settings.model_library_allow_running_scan


def test_model_table_is_virtualized_and_filters_without_row_widgets(qtbot) -> None:
    model = ModelTableModel()
    records = [
        ModelRecord(
            model_id=1, root_id=1, canonical_path=r"D:\模型\a.gguf",
            filename="a.gguf", size=10, mtime_ns=1,
            name="Alpha", architecture="llama", quantization="Q4_K_M",
            tags=["Elin"], reference_count=1,
        ),
        ModelRecord(
            model_id=2, root_id=1, canonical_path=r"D:\模型\b.gguf",
            filename="b.gguf", size=20, mtime_ns=2,
            name="Beta", architecture="qwen", quantization="Q5_K_M",
            reference_count=0,
        ),
    ]
    model.set_records(records)
    proxy = ModelFilterProxy()
    proxy.setSourceModel(model)
    assert isinstance(model, QAbstractTableModel)
    assert model.rowCount() == 2
    proxy.set_filters(text="alpha", reference_filter="referenced")
    assert proxy.rowCount() == 1
    proxy.set_filters(text="", reference_filter="unreferenced")
    assert proxy.rowCount() == 1


class _NoScanScanner:
    def __init__(self) -> None:
        self.calls = 0

    def scan(self, *_args, **_kwargs):
        self.calls += 1
        return ScanSummary(job_id="unexpected", root_ids=[])


def test_page_open_loads_cached_data_without_scan_or_extension_timer(qtbot, tmp_path: Path) -> None:
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    scanner = _NoScanScanner()
    service = ModelLibraryService(tmp_path, store=store, scanner=scanner)
    page = ModelLibraryPage(tmp_path, [], service=service)
    qtbot.addWidget(page)
    page.show()
    assert scanner.calls == 0
    assert page.findChildren(QTimer) == []
    assert page._thread is None
    assert not service.scanning
    page.shutdown()


def test_reference_database_update_is_deferred_while_model_is_active(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    service = ModelLibraryService(tmp_path)
    calls: list[list[ScriptRecord]] = []
    monkeypatch.setattr(
        service, "sync_references", lambda records: calls.append(records)
    )
    records = [script_record(tmp_path, "deferred", model_path="model.gguf")]
    page = ModelLibraryPage(
        tmp_path, records, service=service, service_active=True
    )
    qtbot.addWidget(page)
    assert calls == []
    page.set_model_service_active(False)
    assert calls == [records]
    page.shutdown()


def test_qt_worker_performs_explicit_synthetic_scan_then_exits(
    qtbot, tmp_path: Path
) -> None:
    root = tmp_path / "模型 文件"
    root.mkdir()
    write_model(root / "示例 Q4_K_M.gguf")
    service = ModelLibraryService(tmp_path)
    service.add_root(root)
    page = ModelLibraryPage(tmp_path, [], service=service)
    qtbot.addWidget(page)
    page.show()
    page.scan_all()
    qtbot.waitUntil(lambda: page._thread is None, timeout=10_000)
    assert page.model_model.rowCount() == 1
    indexed = service.store.models()[0]
    assert indexed.name == "测试模型"
    provenance = json.loads(indexed.provenance_json)
    assert provenance["display.name"] == "metadata"
    assert provenance["display.parameter_count"] == "calculated"
    assert not [
        item.path for item in __import__("psutil").Process().open_files()
        if item.path.casefold().endswith(".gguf")
    ]
    assert page.findChildren(QTimer) == []
    page.shutdown()


def test_stale_qt_scan_callback_cannot_replace_newer_status(
    qtbot, tmp_path: Path
) -> None:
    page = ModelLibraryPage(tmp_path, [])
    qtbot.addWidget(page)
    page._expected_job = "new-job"
    page.scan_status.setText("新任务状态")
    page._scan_finished(
        "old-job", ScanSummary(job_id="old-job", root_ids=[])
    )
    assert page.scan_status.text() == "新任务状态"
    page.shutdown()


class _UntilCancelledScanner:
    def scan(self, job_id, roots, cancelled, **_kwargs):
        while not cancelled.wait(0.01):
            pass
        return ScanSummary(
            job_id=job_id, root_ids=[], cancelled=True
        )


def test_page_shutdown_during_scan_cancels_and_joins_worker(
    qtbot, tmp_path: Path
) -> None:
    store = ModelLibraryStore(tmp_path / "model_library.sqlite")
    service = ModelLibraryService(
        tmp_path, store=store, scanner=_UntilCancelledScanner()
    )
    page = ModelLibraryPage(tmp_path, [], service=service)
    qtbot.addWidget(page)
    page.scan_all()
    qtbot.waitUntil(lambda: page._thread is not None)
    page.shutdown()
    assert not service.scanning
    assert service._cancel.is_set()


def test_extension_contains_no_network_client_or_full_hash_import() -> None:
    root = Path("src/llmbatdesk/extensions/model_library")
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in root.glob("*.py")
    )
    assert "httpx" not in source
    assert "requests" not in source
    assert "hashlib" not in source
    assert "mmap" not in source
    assert "cpu_affinity" not in source
    assert "setpriority" not in source


def test_packaging_specs_collect_lazy_extension() -> None:
    for path in (Path("LLMBatDesk.spec"), Path("LLMBatDesk-OneFile.spec")):
        content = path.read_text(encoding="utf-8")
        assert '"llmbatdesk.extensions.model_library.page"' in content
        assert "model_library_hiddenimports" in content
    assert "--smoke-test-model-library" in Path(
        "src/llmbatdesk/qt/app.py"
    ).read_text(encoding="utf-8")
    assert "--smoke-test-model-library" in Path("build.ps1").read_text(
        encoding="utf-8"
    )
