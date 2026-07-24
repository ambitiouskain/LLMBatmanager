from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .domain.models import LaunchModeOverride, ManagedLaunch, Metadata, OperationEvent
from .settings import default_data_dir


class MetadataStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_data_dir() / "llmbatdesk.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    canonical_path TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    favorite INTEGER NOT NULL DEFAULT 0,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    games_json TEXT NOT NULL DEFAULT '[]',
                    category TEXT NOT NULL DEFAULT '',
                    purpose TEXT NOT NULL DEFAULT '',
                    strengths TEXT NOT NULL DEFAULT '',
                    weaknesses TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    last_run_at TEXT
                );
                CREATE TABLE IF NOT EXISTS trust (
                    content_hash TEXT PRIMARY KEY,
                    trusted_at TEXT NOT NULL,
                    working_directory TEXT NOT NULL DEFAULT '',
                    canonical_path TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS managed_launch (
                    launch_id TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL
                );
                """
            )
            metadata_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(metadata)").fetchall()
            }
            if "launch_mode_override" not in metadata_columns:
                db.execute(
                    "ALTER TABLE metadata ADD COLUMN launch_mode_override "
                    "TEXT NOT NULL DEFAULT 'global'"
                )
            trust_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(trust)").fetchall()
            }
            if "working_directory" not in trust_columns:
                db.execute(
                    "ALTER TABLE trust ADD COLUMN working_directory TEXT NOT NULL DEFAULT ''"
                )
            if "canonical_path" not in trust_columns:
                db.execute(
                    "ALTER TABLE trust ADD COLUMN canonical_path TEXT NOT NULL DEFAULT ''"
                )

    def get_metadata(self, canonical_path: str) -> Metadata:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM metadata WHERE canonical_path=?", (canonical_path,)
            ).fetchone()
        if not row:
            return Metadata(canonical_path=canonical_path)
        return Metadata(
            canonical_path=row["canonical_path"], display_name=row["display_name"],
            favorite=bool(row["favorite"]), tags=json.loads(row["tags_json"]),
            games=json.loads(row["games_json"]), category=row["category"],
            purpose=row["purpose"], strengths=row["strengths"], weaknesses=row["weaknesses"],
            notes=row["notes"], sort_order=row["sort_order"],
            last_run_at=datetime.fromisoformat(row["last_run_at"]) if row["last_run_at"] else None,
            launch_mode_override=LaunchModeOverride(
                row["launch_mode_override"] if "launch_mode_override" in row.keys() else "global"
            ),
        )

    def save_metadata(self, metadata: Metadata) -> None:
        values = metadata.model_dump(mode="json")
        with self.connection() as db:
            db.execute(
                """
                INSERT INTO metadata (
                  canonical_path, display_name, favorite, tags_json, games_json,
                  category, purpose, strengths, weaknesses, notes, sort_order,
                  last_run_at, launch_mode_override
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(canonical_path) DO UPDATE SET
                  display_name=excluded.display_name, favorite=excluded.favorite,
                  tags_json=excluded.tags_json, games_json=excluded.games_json,
                  category=excluded.category, purpose=excluded.purpose,
                  strengths=excluded.strengths, weaknesses=excluded.weaknesses,
                  notes=excluded.notes, sort_order=excluded.sort_order,
                  last_run_at=excluded.last_run_at,
                  launch_mode_override=excluded.launch_mode_override
                """,
                (
                    metadata.canonical_path, metadata.display_name, int(metadata.favorite),
                    json.dumps(metadata.tags, ensure_ascii=False),
                    json.dumps(metadata.games, ensure_ascii=False), metadata.category,
                    metadata.purpose, metadata.strengths, metadata.weaknesses, metadata.notes,
                    metadata.sort_order, values["last_run_at"],
                    metadata.launch_mode_override.value,
                ),
            )

    def migrate_path(self, old_path: str, new_path: str) -> bool:
        with self.connection() as db:
            if db.execute("SELECT 1 FROM metadata WHERE canonical_path=?", (new_path,)).fetchone():
                return False
            cursor = db.execute(
                "UPDATE metadata SET canonical_path=? WHERE canonical_path=?", (new_path, old_path)
            )
            return cursor.rowcount == 1

    def trust(
        self, content_hash: str, working_directory: str = "", canonical_path: str = ""
    ) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO trust "
                "(content_hash, trusted_at, working_directory, canonical_path) VALUES (?,?,?,?)",
                (
                    content_hash, datetime.now().isoformat(),
                    working_directory, canonical_path,
                ),
            )

    def is_trusted(self, content_hash: str, working_directory: str = "") -> bool:
        with self.connection() as db:
            row = db.execute(
                "SELECT working_directory FROM trust WHERE content_hash=?", (content_hash,)
            ).fetchone()
        if row is None:
            return False
        saved = str(row["working_directory"] or "")
        if not working_directory:
            return True
        return bool(saved) and saved.casefold() == working_directory.casefold()

    def trust_reason(
        self, content_hash: str, working_directory: str, canonical_path: str
    ) -> str | None:
        with self.connection() as db:
            exact = db.execute(
                "SELECT working_directory FROM trust WHERE content_hash=?",
                (content_hash,),
            ).fetchone()
            prior_path = db.execute(
                "SELECT 1 FROM trust WHERE canonical_path=? LIMIT 1",
                (canonical_path,),
            ).fetchone()
        if exact is not None:
            saved = str(exact["working_directory"] or "")
            if saved and saved.casefold() != working_directory.casefold():
                return "工作目录已变化"
            if not saved:
                return "新脚本，尚未信任"
            return None
        return "脚本内容已变化" if prior_path else "新脚本，尚未信任"

    def add_event(self, event: OperationEvent) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO history(kind,timestamp,data_json) VALUES (?,?,?)",
                (event.kind, event.timestamp.isoformat(),
                 json.dumps(event.data, ensure_ascii=False, default=str)),
            )

    def list_events(self, kinds: set[str] | None = None, limit: int = 500) -> list[OperationEvent]:
        with self.connection() as db:
            if kinds:
                placeholders = ",".join("?" for _ in kinds)
                rows = db.execute(
                    f"SELECT kind,timestamp,data_json FROM history "
                    f"WHERE kind IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                    (*sorted(kinds), limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT kind,timestamp,data_json FROM history ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            OperationEvent(
                kind=row["kind"],
                timestamp=datetime.fromisoformat(row["timestamp"]),
                data=json.loads(row["data_json"]),
            )
            for row in rows
        ]

    def save_launch(self, launch: ManagedLaunch) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO managed_launch VALUES (?,?)",
                (launch.launch_id, launch.model_dump_json()),
            )

    def load_launches(self) -> list[ManagedLaunch]:
        with self.connection() as db:
            rows = db.execute("SELECT data_json FROM managed_launch").fetchall()
        return [ManagedLaunch.model_validate_json(row["data_json"]) for row in rows]

    def remove_launch(self, launch_id: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM managed_launch WHERE launch_id=?", (launch_id,))

    def history_count(self) -> int:
        with self.connection() as db:
            return int(db.execute("SELECT COUNT(*) FROM history").fetchone()[0])

    def clear_history(self) -> int:
        with self.connection() as db:
            count = int(db.execute("SELECT COUNT(*) FROM history").fetchone()[0])
            db.execute("DELETE FROM history")
        return count
