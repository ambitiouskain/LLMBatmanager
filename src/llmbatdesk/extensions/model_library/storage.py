from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .models import (
    AssociatedComponent, ModelFileRole, ModelRecord, ModelStatus, ParseStatus,
    ReferenceState, ScanRoot, ReferenceRole, ScriptReference,
)
from .presentation import friendly_architecture, model_role_from_reference


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS scan_roots (
    id INTEGER PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    recursive INTEGER NOT NULL DEFAULT 1,
    last_scan_at TEXT,
    indexed_count INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 1,
    status_message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES scan_roots(id) ON DELETE CASCADE,
    canonical_path TEXT NOT NULL UNIQUE COLLATE NOCASE,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    status TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    parse_error TEXT NOT NULL DEFAULT '',
    metadata_reader_version INTEGER NOT NULL DEFAULT 0,
    metadata_schema_version INTEGER NOT NULL DEFAULT 0,
    last_parse_outcome_class TEXT NOT NULL DEFAULT 'legacy',
    name TEXT NOT NULL DEFAULT '',
    architecture TEXT NOT NULL DEFAULT '',
    parameter_count INTEGER,
    active_parameter_count INTEGER,
    nominal_size TEXT NOT NULL DEFAULT '',
    size_provenance TEXT NOT NULL DEFAULT 'unknown',
    quantization TEXT NOT NULL DEFAULT '',
    file_type INTEGER,
    size_label TEXT NOT NULL DEFAULT '',
    tokenizer_family TEXT NOT NULL DEFAULT '',
    context_length INTEGER,
    tensor_count INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    tags_json TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '',
    possible_duplicate INTEGER NOT NULL DEFAULT 0,
    model_role TEXT NOT NULL DEFAULT 'unknown',
    role_provenance TEXT NOT NULL DEFAULT 'unknown',
    metadata_role TEXT NOT NULL DEFAULT 'unknown',
    metadata_role_provenance TEXT NOT NULL DEFAULT 'unknown',
    last_scanned_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_models_root_status ON models(root_id, status);
CREATE INDEX IF NOT EXISTS idx_models_filter ON models(architecture, quantization, status);
CREATE INDEX IF NOT EXISTS idx_models_name ON models(name);
CREATE TABLE IF NOT EXISTS script_references (
    id INTEGER PRIMARY KEY,
    script_path TEXT NOT NULL COLLATE NOCASE,
    script_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'primary',
    state TEXT NOT NULL,
    model_path TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_refs_model_path ON script_references(model_path COLLATE NOCASE);
"""

MAX_TAGS = 64
MAX_TAG_LENGTH = 64
MAX_NOTES_LENGTH = 32_000


def _is_corruption(error: sqlite3.DatabaseError) -> bool:
    value = str(error).casefold()
    return any(
        marker in value
        for marker in (
            "not a database", "database disk image is malformed",
            "file is encrypted", "malformed database schema",
        )
    )


def _safe_json_list(value: str, *, limit: int = 256) -> list[str]:
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        return []
    if not isinstance(result, list):
        return []
    return [str(item)[:1024] for item in result[:limit]]


def _safe_json_object(value: str) -> str:
    try:
        result = json.loads(value)
    except (ValueError, TypeError):
        return "{}"
    if not isinstance(result, dict):
        return "{}"
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


class ModelLibraryStore:
    """Isolated, lazily instantiated model-index database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialize_with_recovery()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize_with_recovery(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as connection:
                connection.executescript(SCHEMA)
                self._migrate_model_schema(connection)
                self._migrate_reference_schema(connection)
                self._refresh_reference_roles(connection)
                checked = connection.execute("PRAGMA quick_check").fetchone()
                if not checked or checked[0] != "ok":
                    raise sqlite3.DatabaseError(
                        f"database disk image is malformed: {checked}"
                    )
        except sqlite3.DatabaseError as error:
            if not _is_corruption(error):
                raise
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            for suffix in ("", "-wal", "-shm"):
                source = Path(str(self.path) + suffix)
                if source.exists():
                    source.replace(
                        self.path.with_name(
                            f"{self.path.name}.corrupt-{stamp}{suffix}"
                        )
                    )
            with self._connect() as connection:
                connection.executescript(SCHEMA)
                self._migrate_model_schema(connection)
                self._migrate_reference_schema(connection)
                self._refresh_reference_roles(connection)

    @staticmethod
    def _migrate_model_schema(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(models)").fetchall()
        }
        additions = {
            "metadata_reader_version": "INTEGER NOT NULL DEFAULT 0",
            "metadata_schema_version": "INTEGER NOT NULL DEFAULT 0",
            "last_parse_outcome_class": "TEXT NOT NULL DEFAULT 'legacy'",
            "active_parameter_count": "INTEGER",
            "nominal_size": "TEXT NOT NULL DEFAULT ''",
            "size_provenance": "TEXT NOT NULL DEFAULT 'unknown'",
            "file_type": "INTEGER",
            "model_role": "TEXT NOT NULL DEFAULT 'unknown'",
            "role_provenance": "TEXT NOT NULL DEFAULT 'unknown'",
            "metadata_role": "TEXT NOT NULL DEFAULT 'unknown'",
            "metadata_role_provenance": "TEXT NOT NULL DEFAULT 'unknown'",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE models ADD COLUMN {name} {declaration}"
                )

    @staticmethod
    def _migrate_reference_schema(connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(script_references)"
            ).fetchall()
        }
        if "role" in columns and "id" in columns:
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_refs_identity
                ON script_references(
                    script_path COLLATE NOCASE, role, state,
                    model_path COLLATE NOCASE
                )
                """
            )
            return
        connection.execute("DROP INDEX IF EXISTS idx_refs_model_path")
        connection.execute("DROP INDEX IF EXISTS idx_refs_identity")
        connection.execute(
            "ALTER TABLE script_references RENAME TO script_references_legacy"
        )
        connection.executescript(
            """
            CREATE TABLE script_references (
                id INTEGER PRIMARY KEY,
                script_path TEXT NOT NULL COLLATE NOCASE,
                script_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'primary',
                state TEXT NOT NULL,
                model_path TEXT NOT NULL DEFAULT '',
                candidates_json TEXT NOT NULL DEFAULT '[]',
                reason TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX idx_refs_identity
            ON script_references(
                script_path COLLATE NOCASE, role, state,
                model_path COLLATE NOCASE
            );
            CREATE INDEX idx_refs_model_path
            ON script_references(model_path COLLATE NOCASE);
            INSERT INTO script_references(
                script_path, script_name, role, state, model_path,
                candidates_json, reason
            )
            SELECT script_path, script_name, 'primary',
                CASE WHEN state='multiple' THEN 'ambiguous' ELSE state END,
                model_path, candidates_json, reason
            FROM script_references_legacy;
            DROP TABLE script_references_legacy;
            """
        )

    def close(self) -> None:
        # Connections are operation-scoped; there is intentionally no idle
        # database handle or background write loop.
        return

    def add_root(
        self, canonical_path: str, display_name: str, recursive: bool = True
    ) -> ScanRoot:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scan_roots(canonical_path, display_name, recursive)
                VALUES(?, ?, ?)
                ON CONFLICT(canonical_path) DO UPDATE SET
                    display_name=excluded.display_name,
                    recursive=excluded.recursive
                """,
                (canonical_path, display_name[:256], int(recursive)),
            )
            row = connection.execute(
                "SELECT * FROM scan_roots WHERE canonical_path=?",
                (canonical_path,),
            ).fetchone()
        return self._root(row)

    def add_root_with_status(
        self, canonical_path: str, display_name: str, recursive: bool = True
    ) -> tuple[ScanRoot, bool]:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO scan_roots(
                    canonical_path, display_name, recursive
                ) VALUES(?, ?, ?)
                """,
                (canonical_path, display_name[:256], int(recursive)),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM scan_roots WHERE canonical_path=?",
                (canonical_path,),
            ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("扫描目录写入后无法读取")
        return self._root(row), created

    def remove_root(self, root_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM scan_roots WHERE id=?", (root_id,))

    def update_root(
        self, root_id: int, *, enabled: bool | None = None,
        recursive: bool | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[object] = []
        if enabled is not None:
            assignments.append("enabled=?")
            values.append(int(enabled))
        if recursive is not None:
            assignments.append("recursive=?")
            values.append(int(recursive))
        if not assignments:
            return
        values.append(root_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE scan_roots SET {', '.join(assignments)} WHERE id=?",
                values,
            )

    def roots(self, *, enabled_only: bool = False) -> list[ScanRoot]:
        query = "SELECT * FROM scan_roots"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY display_name COLLATE NOCASE, canonical_path COLLATE NOCASE"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [self._root(row) for row in rows]

    def root(self, root_id: int) -> ScanRoot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scan_roots WHERE id=?", (root_id,)
            ).fetchone()
        return self._root(row) if row else None

    @staticmethod
    def _root(row: sqlite3.Row) -> ScanRoot:
        return ScanRoot(
            root_id=row["id"], canonical_path=row["canonical_path"],
            display_name=row["display_name"], enabled=bool(row["enabled"]),
            recursive=bool(row["recursive"]), last_scan_at=row["last_scan_at"],
            indexed_count=row["indexed_count"], available=bool(row["available"]),
            status_message=row["status_message"],
        )

    @contextmanager
    def scan_session(self) -> Iterator[sqlite3.Connection]:
        """One bounded transaction scope for a single sequential scan."""
        with self._connect() as connection:
            yield connection

    def cached_identity(
        self, canonical_path: str,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[int, int] | None:
        if connection is None:
            with self._connect() as owned:
                return self.cached_identity(canonical_path, owned)
        row = connection.execute(
            "SELECT size, mtime_ns FROM models WHERE canonical_path=?",
            (canonical_path,),
        ).fetchone()
        return (row["size"], row["mtime_ns"]) if row else None

    def cached_parse_state(
        self, canonical_path: str,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[int, int, int, int, str] | None:
        if connection is None:
            with self._connect() as owned:
                return self.cached_parse_state(canonical_path, owned)
        row = connection.execute(
            """
            SELECT size, mtime_ns, metadata_reader_version,
                   metadata_schema_version, last_parse_outcome_class
            FROM models WHERE canonical_path=?
            """,
            (canonical_path,),
        ).fetchone()
        return (
            row["size"], row["mtime_ns"], row["metadata_reader_version"],
            row["metadata_schema_version"], row["last_parse_outcome_class"],
        ) if row else None

    def stale_cache_count(
        self, reader_version: int, schema_version: int,
        root_ids: list[int] | None = None,
    ) -> int:
        where = (
            "metadata_reader_version<? OR metadata_schema_version<?"
        )
        values: list[object] = [reader_version, schema_version]
        if root_ids:
            placeholders = ",".join("?" for _ in root_ids)
            where = f"({where}) AND root_id IN ({placeholders})"
            values.extend(root_ids)
        with self._connect() as connection:
            return int(connection.execute(
                f"SELECT COUNT(*) FROM models WHERE {where}", values
            ).fetchone()[0])

    def upsert_model(
        self, record: ModelRecord,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if connection is None:
            with self._connect() as owned:
                self.upsert_model(record, owned)
            return
        connection.execute(
            """
                INSERT INTO models(
                    root_id, canonical_path, filename, size, mtime_ns, status,
                    parse_status, parse_error, metadata_reader_version,
                    metadata_schema_version, last_parse_outcome_class,
                    name, architecture, parameter_count, active_parameter_count,
                    nominal_size, size_provenance, quantization, file_type,
                    size_label, tokenizer_family,
                    context_length, tensor_count, metadata_json, provenance_json,
                    model_role, role_provenance, metadata_role,
                    metadata_role_provenance, last_scanned_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(canonical_path) DO UPDATE SET
                    root_id=excluded.root_id, filename=excluded.filename,
                    size=excluded.size, mtime_ns=excluded.mtime_ns,
                    status=excluded.status, parse_status=excluded.parse_status,
                    parse_error=excluded.parse_error,
                    metadata_reader_version=excluded.metadata_reader_version,
                    metadata_schema_version=excluded.metadata_schema_version,
                    last_parse_outcome_class=excluded.last_parse_outcome_class,
                    name=excluded.name,
                    architecture=excluded.architecture,
                    parameter_count=excluded.parameter_count,
                    active_parameter_count=excluded.active_parameter_count,
                    nominal_size=excluded.nominal_size,
                    size_provenance=excluded.size_provenance,
                    quantization=excluded.quantization,
                    file_type=excluded.file_type,
                    size_label=excluded.size_label,
                    tokenizer_family=excluded.tokenizer_family,
                    context_length=excluded.context_length,
                    tensor_count=excluded.tensor_count,
                    metadata_json=excluded.metadata_json,
                    provenance_json=excluded.provenance_json,
                    model_role=excluded.model_role,
                    role_provenance=excluded.role_provenance,
                    metadata_role=excluded.metadata_role,
                    metadata_role_provenance=excluded.metadata_role_provenance,
                    last_scanned_at=excluded.last_scanned_at
            """,
            (
                record.root_id, record.canonical_path, record.filename,
                record.size, record.mtime_ns, record.status.value,
                record.parse_status.value, record.parse_error[:4000],
                record.metadata_reader_version,
                record.metadata_schema_version,
                record.last_parse_outcome_class[:64],
                record.name[:1024], record.architecture[:256],
                record.parameter_count, record.active_parameter_count,
                record.nominal_size[:128], record.size_provenance[:256],
                record.quantization[:256], record.file_type,
                record.size_label[:128], record.tokenizer_family[:256],
                record.context_length, record.tensor_count,
                record.metadata_json, record.provenance_json,
                record.model_role.value, record.role_provenance[:256],
                record.metadata_role.value,
                record.metadata_role_provenance[:256],
                record.last_scanned_at,
            ),
        )

    def mark_missing_after_scan(
        self, root_id: int, seen_paths: set[str], scanned_at: str
    ) -> int:
        normalized_seen = {value.casefold() for value in seen_paths}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT canonical_path FROM models WHERE root_id=? AND status<>?",
                (root_id, ModelStatus.MISSING.value),
            ).fetchall()
            missing = [
                row["canonical_path"] for row in rows
                if row["canonical_path"].casefold() not in normalized_seen
            ]
            connection.executemany(
                """
                UPDATE models SET status=?, last_scanned_at=?
                WHERE canonical_path=?
                """,
                [
                    (ModelStatus.MISSING.value, scanned_at, path)
                    for path in missing
                ],
            )
        return len(missing)

    def complete_root_scan(
        self, root_id: int, *, available: bool, message: str,
        scanned_at: str | None,
    ) -> None:
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM models WHERE root_id=?",
                (root_id,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE scan_roots SET last_scan_at=?, indexed_count=?,
                    available=?, status_message=? WHERE id=?
                """,
                (scanned_at, count, int(available), message[:2000], root_id),
            )
            self._refresh_duplicate_flags(connection)
            self._refresh_reference_roles(connection)

    def models(
        self, *, limit: int = 500, offset: int = 0,
        search: str = "",
    ) -> list[ModelRecord]:
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        where = ""
        values: list[object] = []
        if search:
            where = (
                "WHERE m.name LIKE ? ESCAPE '\\' OR m.filename LIKE ? ESCAPE '\\' "
                "OR m.canonical_path LIKE ? ESCAPE '\\' OR m.architecture LIKE ? ESCAPE '\\' "
                "OR m.tags_json LIKE ? ESCAPE '\\'"
            )
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            values.extend([f"%{escaped}%"] * 5)
        query = f"""
            SELECT m.*,
                (SELECT COUNT(*) FROM script_references r
                 WHERE r.model_path=m.canonical_path COLLATE NOCASE
                 AND r.state IN ('explicit','missing')) AS ref_count,
                (SELECT COUNT(*) FROM script_references r
                 WHERE r.model_path=m.canonical_path COLLATE NOCASE
                 AND r.state IN ('explicit','missing')
                 AND r.role='primary') AS primary_ref_count,
                (SELECT COUNT(*) FROM script_references r
                 WHERE r.model_path=m.canonical_path COLLATE NOCASE
                 AND r.state IN ('explicit','missing')
                 AND r.role<>'primary') AS auxiliary_ref_count,
                (SELECT group_concat(r.script_name, char(31)) FROM script_references r
                 WHERE r.model_path=m.canonical_path COLLATE NOCASE
                 AND r.state IN ('explicit','missing')) AS scripts
            FROM models m {where}
            ORDER BY COALESCE(NULLIF(m.name,''),m.filename) COLLATE NOCASE
            LIMIT ? OFFSET ?
        """
        values.extend([limit, offset])
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._model(row) for row in rows]

    def auxiliary_count(self) -> int:
        values = tuple(
            role.value for role in (
                ModelFileRole.MMPROJ, ModelFileRole.DRAFT,
                ModelFileRole.ADAPTER, ModelFileRole.CONTROL,
                ModelFileRole.AUXILIARY,
            )
        )
        with self._connect() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM models WHERE model_role IN (?,?,?,?,?)",
                values,
            ).fetchone()[0])

    def model(self, model_id: int) -> ModelRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT m.*,
                    (SELECT COUNT(*) FROM script_references r
                     WHERE r.model_path=m.canonical_path COLLATE NOCASE
                     AND r.state IN ('explicit','missing')) AS ref_count,
                    (SELECT COUNT(*) FROM script_references r
                     WHERE r.model_path=m.canonical_path COLLATE NOCASE
                     AND r.state IN ('explicit','missing')
                     AND r.role='primary') AS primary_ref_count,
                    (SELECT COUNT(*) FROM script_references r
                     WHERE r.model_path=m.canonical_path COLLATE NOCASE
                     AND r.state IN ('explicit','missing')
                     AND r.role<>'primary') AS auxiliary_ref_count,
                    (SELECT group_concat(r.script_name, char(31)) FROM script_references r
                     WHERE r.model_path=m.canonical_path COLLATE NOCASE
                     AND r.state IN ('explicit','missing')) AS scripts
                FROM models m WHERE m.id=?
                """,
                (model_id,),
            ).fetchone()
        return self._model(row) if row else None

    @staticmethod
    def _model(row: sqlite3.Row) -> ModelRecord:
        tags = _safe_json_list(row["tags_json"], limit=MAX_TAGS)
        scripts = str(row["scripts"] or "").split(chr(31)) if row["scripts"] else []
        try:
            status = ModelStatus(row["status"])
        except ValueError:
            status = ModelStatus.ERROR
        try:
            parse_status = ParseStatus(row["parse_status"])
        except ValueError:
            parse_status = ParseStatus.ERROR
        record = ModelRecord(
            model_id=row["id"], root_id=row["root_id"],
            canonical_path=row["canonical_path"], filename=row["filename"],
            size=row["size"], mtime_ns=row["mtime_ns"],
            status=status,
            parse_status=parse_status,
            parse_error=row["parse_error"], name=row["name"],
            metadata_reader_version=row["metadata_reader_version"],
            metadata_schema_version=row["metadata_schema_version"],
            last_parse_outcome_class=row["last_parse_outcome_class"],
            architecture=row["architecture"],
            parameter_count=row["parameter_count"],
            active_parameter_count=row["active_parameter_count"],
            nominal_size=row["nominal_size"],
            size_provenance=row["size_provenance"],
            quantization=row["quantization"], size_label=row["size_label"],
            file_type=row["file_type"],
            tokenizer_family=row["tokenizer_family"],
            context_length=row["context_length"],
            tensor_count=row["tensor_count"],
            metadata_json=_safe_json_object(row["metadata_json"]),
            provenance_json=_safe_json_object(row["provenance_json"]), tags=tags,
            notes=str(row["notes"] or "")[:MAX_NOTES_LENGTH],
            reference_count=row["ref_count"], related_scripts=scripts,
            primary_reference_count=row["primary_ref_count"],
            auxiliary_reference_count=row["auxiliary_ref_count"],
            possible_duplicate=bool(row["possible_duplicate"]),
            model_role=ModelLibraryStore._safe_model_role(row["model_role"]),
            role_provenance=row["role_provenance"],
            metadata_role=ModelLibraryStore._safe_model_role(
                row["metadata_role"]
            ),
            metadata_role_provenance=row["metadata_role_provenance"],
            last_scanned_at=row["last_scanned_at"],
        )
        (
            record.architecture_family,
            record.architecture_provenance,
        ) = friendly_architecture(record.architecture)
        return record

    def save_user_metadata(
        self, model_id: int, tags: list[str], notes: str
    ) -> None:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in tags[:MAX_TAGS]:
            value = str(raw).strip()[:MAX_TAG_LENGTH]
            if value and value.casefold() not in seen:
                normalized.append(value)
                seen.add(value.casefold())
        with self._connect() as connection:
            connection.execute(
                "UPDATE models SET tags_json=?, notes=? WHERE id=?",
                (
                    json.dumps(normalized, ensure_ascii=False),
                    notes[:MAX_NOTES_LENGTH],
                    model_id,
                ),
            )

    @staticmethod
    def _safe_model_role(value: str) -> ModelFileRole:
        try:
            return ModelFileRole(value)
        except ValueError:
            return ModelFileRole.UNKNOWN

    def replace_references(self, references: list[ScriptReference]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM script_references")
            connection.executemany(
                """
                INSERT INTO script_references(
                    script_path, script_name, role, state, model_path,
                    candidates_json, reason
                ) VALUES(?,?,?,?,?,?,?)
                """,
                [
                    (
                        item.script_path, item.script_name, item.role.value,
                        item.state.value,
                        item.model_path,
                        json.dumps(item.candidates[:16], ensure_ascii=False),
                        item.reason[:2000],
                    )
                    for item in references
                ],
            )
            self._refresh_duplicate_flags(connection)
            self._refresh_reference_roles(connection)

    def refresh_reference_roles(self) -> None:
        with self._connect() as connection:
            self._refresh_reference_roles(connection)

    @staticmethod
    def _refresh_reference_roles(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE models SET model_role=metadata_role,
                role_provenance=metadata_role_provenance
            """
        )
        for reference_role in (
            ReferenceRole.AUXILIARY,
            ReferenceRole.CONTROL,
            ReferenceRole.ADAPTER,
            ReferenceRole.DRAFT,
            ReferenceRole.MMPROJ,
            ReferenceRole.PRIMARY,
        ):
            model_role = model_role_from_reference(reference_role)
            connection.execute(
                """
                UPDATE models SET model_role=?, role_provenance=?
                WHERE EXISTS(
                    SELECT 1 FROM script_references r
                    WHERE r.model_path=models.canonical_path COLLATE NOCASE
                      AND r.role=?
                      AND r.state IN ('explicit','missing')
                )
                """,
                (
                    model_role.value, "script reference",
                    reference_role.value,
                ),
            )

    def references_for_model(self, canonical_path: str) -> list[ScriptReference]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM script_references WHERE model_path=? COLLATE NOCASE",
                (canonical_path,),
            ).fetchall()
        return [
            ScriptReference(
                script_path=row["script_path"], script_name=row["script_name"],
                state=ReferenceState(row["state"]), model_path=row["model_path"],
                role=ReferenceRole(row["role"]),
                candidates=_safe_json_list(row["candidates_json"], limit=16),
                reason=row["reason"],
            )
            for row in rows
        ]

    def components_for_primary(
        self, canonical_path: str
    ) -> list[AssociatedComponent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT
                    r2.model_path, r2.role, r2.state,
                    r2.script_path, r2.script_name,
                    m.id AS model_id, m.filename
                FROM script_references p
                JOIN script_references r2
                  ON r2.script_path=p.script_path COLLATE NOCASE
                LEFT JOIN models m
                  ON m.canonical_path=r2.model_path COLLATE NOCASE
                WHERE p.model_path=? COLLATE NOCASE
                  AND p.role='primary'
                  AND p.state IN ('explicit','missing')
                  AND r2.role IN (
                      'mmproj','draft','adapter','control','auxiliary'
                  )
                  AND r2.model_path<>''
                ORDER BY r2.script_name COLLATE NOCASE,
                         COALESCE(m.filename,r2.model_path) COLLATE NOCASE
                """,
                (canonical_path,),
            ).fetchall()
        result: list[AssociatedComponent] = []
        for row in rows:
            reference_role = ReferenceRole(row["role"])
            result.append(AssociatedComponent(
                canonical_path=row["model_path"],
                filename=row["filename"] or Path(row["model_path"]).name,
                role=model_role_from_reference(reference_role),
                script_path=row["script_path"],
                script_name=row["script_name"],
                existence_state=(
                    "文件存在" if row["model_id"] is not None
                    else "文件缺失" if row["state"] == "missing"
                    else "未索引"
                ),
                confidence="明确脚本参数",
                model_id=row["model_id"],
            ))
        return result

    def primaries_for_component(
        self, canonical_path: str
    ) -> list[AssociatedComponent]:
        """Return every verified primary paired with an auxiliary via a script."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT
                    p.model_path, p.state,
                    p.script_path, p.script_name,
                    m.id AS model_id, m.filename
                FROM script_references auxiliary
                JOIN script_references p
                  ON p.script_path=auxiliary.script_path COLLATE NOCASE
                LEFT JOIN models m
                  ON m.canonical_path=p.model_path COLLATE NOCASE
                WHERE auxiliary.model_path=? COLLATE NOCASE
                  AND auxiliary.role IN (
                      'mmproj','draft','adapter','control','auxiliary'
                  )
                  AND auxiliary.state IN ('explicit','missing')
                  AND p.role='primary'
                  AND p.state IN ('explicit','missing')
                  AND p.model_path<>''
                ORDER BY p.script_name COLLATE NOCASE,
                         COALESCE(m.filename,p.model_path) COLLATE NOCASE
                """,
                (canonical_path,),
            ).fetchall()
        return [
            AssociatedComponent(
                canonical_path=row["model_path"],
                filename=row["filename"] or Path(row["model_path"]).name,
                role=ModelFileRole.PRIMARY,
                script_path=row["script_path"],
                script_name=row["script_name"],
                existence_state=(
                    "文件存在" if row["model_id"] is not None
                    else "文件缺失" if row["state"] == "missing"
                    else "未索引"
                ),
                confidence=(
                    "明确脚本参数"
                    if row["state"] == "explicit"
                    else "脚本参数（文件缺失）"
                ),
                model_id=row["model_id"],
            )
            for row in rows
        ]

    def problem_references(self, limit: int = 200) -> list[ScriptReference]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM script_references
                WHERE state IN ('missing','multiple','ambiguous','dynamic','unparsed')
                ORDER BY script_name COLLATE NOCASE LIMIT ?
                """,
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [
            ScriptReference(
                script_path=row["script_path"], script_name=row["script_name"],
                state=ReferenceState(row["state"]),
                role=ReferenceRole(row["role"]),
                model_path=row["model_path"],
                candidates=_safe_json_list(row["candidates_json"], limit=16),
                reason=row["reason"],
            )
            for row in rows
        ]

    @staticmethod
    def _refresh_duplicate_flags(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE models SET possible_duplicate=0")
        connection.execute(
            """
            UPDATE models SET possible_duplicate=1
            WHERE status=? AND (size, lower(COALESCE(NULLIF(name,''),filename)),
                architecture, COALESCE(parameter_count,-1), quantization) IN (
                SELECT size, lower(COALESCE(NULLIF(name,''),filename)),
                    architecture, COALESCE(parameter_count,-1), quantization
                FROM models WHERE status=?
                GROUP BY size, lower(COALESCE(NULLIF(name,''),filename)),
                    architecture, COALESCE(parameter_count,-1), quantization
                HAVING COUNT(*) > 1
            )
            """,
            (ModelStatus.AVAILABLE.value, ModelStatus.AVAILABLE.value),
        )
