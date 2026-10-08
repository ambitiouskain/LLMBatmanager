from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

from ...domain.models import RuntimeState, ScriptRecord
from .models import (
    ModelRecord, ScanRoot, ScanSummary, ScanSuppressedError,
)
from .references import references_from_records
from .scanner import ModelScanner, canonical_scan_root
from .storage import ModelLibraryStore
from .gguf_reader import METADATA_READER_VERSION, METADATA_SCHEMA_VERSION


MODEL_OPERATION_STATES = {
    RuntimeState.STARTING,
    RuntimeState.PROCESS_RUNNING_PORT_CLOSED,
    RuntimeState.PORT_LISTENING_API_NOT_READY,
    RuntimeState.API_READY,
    RuntimeState.AUTH_REQUIRED,
    RuntimeState.STOPPING,
    RuntimeState.DETACHED_UNVERIFIED,
}


class ModelLibraryService:
    """Isolated extension service; instantiated only when the tab is opened."""

    def __init__(
        self,
        data_dir: Path,
        *,
        allow_running_scan: bool = False,
        store: ModelLibraryStore | None = None,
        scanner: ModelScanner | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.store = store or ModelLibraryStore(
            data_dir / "model_library.sqlite"
        )
        self.scanner = scanner or ModelScanner(self.store)
        self.allow_running_scan = allow_running_scan
        self._service_active = False
        self._cancel = threading.Event()
        self._guard = threading.Lock()
        self._active_job_id: str | None = None
        self._generation = 0
        self._closed = False

    @property
    def scanning(self) -> bool:
        with self._guard:
            return self._active_job_id is not None

    @property
    def active_job_id(self) -> str | None:
        with self._guard:
            return self._active_job_id

    def set_service_active(self, active: bool) -> bool:
        self._service_active = active
        if active and not self.allow_running_scan and self.scanning:
            self.cancel_scan()
            return True
        return False

    def add_root(
        self, path: Path, *, display_name: str = "", recursive: bool = True
    ) -> ScanRoot:
        canonical = canonical_scan_root(path)
        return self.store.add_root(
            str(canonical), display_name.strip() or canonical.name or str(canonical),
            recursive,
        )

    def add_root_with_status(
        self, path: Path, *, display_name: str = "", recursive: bool = True
    ) -> tuple[ScanRoot, bool]:
        canonical = canonical_scan_root(path)
        return self.store.add_root_with_status(
            str(canonical),
            display_name.strip() or canonical.name or str(canonical),
            recursive,
        )

    def remove_root(self, root_id: int) -> None:
        if self.scanning:
            raise RuntimeError("扫描期间不能移除目录")
        self.store.remove_root(root_id)

    def roots(self) -> list[ScanRoot]:
        return self.store.roots()

    def models(
        self, *, search: str = "", limit: int = 500, offset: int = 0
    ) -> list[ModelRecord]:
        return self.store.models(search=search, limit=limit, offset=offset)

    def model(self, model_id: int) -> ModelRecord | None:
        return self.store.model(model_id)

    def stale_cache_count(self, root_ids: list[int] | None = None) -> int:
        return self.store.stale_cache_count(
            METADATA_READER_VERSION, METADATA_SCHEMA_VERSION, root_ids
        )

    def save_user_metadata(
        self, model_id: int, tags: list[str], notes: str
    ) -> None:
        self.store.save_user_metadata(model_id, tags, notes)

    def sync_references(self, records: list[ScriptRecord]) -> None:
        self.store.replace_references(references_from_records(records))

    def begin_scan(self, root_ids: list[int] | None = None) -> str:
        if self._closed:
            raise RuntimeError("模型库已关闭")
        if self._service_active and not self.allow_running_scan:
            raise ScanSuppressedError(
                "为避免影响模型性能，模型运行期间已暂停扫描"
            )
        with self._guard:
            if self._active_job_id is not None:
                raise RuntimeError("已有模型扫描正在进行")
            self._generation += 1
            job_id = f"scan-{self._generation}-{uuid4().hex}"
            self._active_job_id = job_id
            self._cancel = threading.Event()
        return job_id

    def run_scan(
        self, job_id: str, root_ids: list[int] | None = None,
        *, batch_callback=None,
    ) -> ScanSummary:
        with self._guard:
            if job_id != self._active_job_id:
                raise RuntimeError("扫描任务已经失效")
            cancel = self._cancel
        roots = self.store.roots(enabled_only=True)
        if root_ids is not None:
            wanted = set(root_ids)
            roots = [root for root in roots if root.root_id in wanted]
        try:
            return self.scanner.scan(
                job_id, roots, cancel, batch_callback=batch_callback
            )
        finally:
            with self._guard:
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def cancel_scan(self) -> None:
        with self._guard:
            if self._active_job_id is not None:
                self._cancel.set()
                # Invalidate callbacks immediately; the worker remains
                # cooperative and closes its current file before returning.
                self._generation += 1

    def accepts_result(self, job_id: str) -> bool:
        with self._guard:
            return not self._closed and (
                self._active_job_id == job_id
                or self._active_job_id is None
                and job_id.startswith(f"scan-{self._generation}-")
            )

    def rescan_one(self, model_id: int) -> ModelRecord:
        if self._service_active and not self.allow_running_scan:
            raise ScanSuppressedError(
                "为避免影响模型性能，模型运行期间已暂停扫描"
            )
        model = self.store.model(model_id)
        if model is None:
            raise ValueError("模型记录不存在")
        root = self.store.root(model.root_id)
        if root is None:
            raise ValueError("扫描目录记录不存在")
        # A one-file refresh still uses the same bounded reader and is explicit.
        from datetime import datetime
        from .scanner import _record_from_metadata

        path = Path(model.canonical_path)
        file_stat = path.stat()
        record = _record_from_metadata(
            model.root_id, path, file_stat,
            datetime.now().astimezone().isoformat(),
        )
        self.store.upsert_model(record)
        self.store.refresh_reference_roles()
        refreshed = self.store.model(model_id)
        if refreshed is None:
            raise RuntimeError("元数据刷新后记录丢失")
        return refreshed

    def run_rescan_one(self, job_id: str, model_id: int) -> ModelRecord:
        with self._guard:
            if job_id != self._active_job_id:
                raise RuntimeError("扫描任务已经失效")
        try:
            return self.rescan_one(model_id)
        finally:
            with self._guard:
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def shutdown(self) -> None:
        self._closed = True
        self.cancel_scan()
        self.store.close()
