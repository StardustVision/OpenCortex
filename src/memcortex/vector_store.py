from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3


@dataclass
class VectorRecord:
    event_id: str
    content: str
    vector: list[float]
    source_tool: str
    event_type: str
    session_id: str
    meta_json: str


class BaseVectorStore:
    def upsert(self, record: VectorRecord) -> None:
        raise NotImplementedError


class SQLiteVectorStore(BaseVectorStore):
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_records (
                    event_id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    source_tool TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                """
            )

    def upsert(self, record: VectorRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_records(
                    event_id, content, vector_json, source_tool, event_type, session_id, meta_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CAST(strftime("%s", "now") AS REAL))
                ON CONFLICT(event_id) DO UPDATE SET
                    content=excluded.content,
                    vector_json=excluded.vector_json,
                    source_tool=excluded.source_tool,
                    event_type=excluded.event_type,
                    session_id=excluded.session_id,
                    meta_json=excluded.meta_json,
                    updated_at=CAST(strftime("%s", "now") AS REAL)
                """,
                (
                    record.event_id,
                    record.content,
                    json.dumps(record.vector),
                    record.source_tool,
                    record.event_type,
                    record.session_id,
                    record.meta_json,
                ),
            )


class LanceDBVectorStore(BaseVectorStore):
    def __init__(self, uri: str, table_name: str = "memory_records"):
        try:
            import lancedb
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "lancedb package not installed. Install it before using LanceDB backend."
            ) from exc
        self._lancedb = lancedb
        self._db = self._lancedb.connect(uri)
        self._table_name = table_name
        self._table = None

    def _ensure_table(self):
        if self._table is not None:
            return
        names = set(self._db.table_names())
        if self._table_name in names:
            self._table = self._db.open_table(self._table_name)
            return

        self._table = self._db.create_table(
            self._table_name,
            data=[
                {
                    "event_id": "init",
                    "vector": [0.0] * 16,
                    "content": "init",
                    "source_tool": "init",
                    "event_type": "init",
                    "session_id": "init",
                    "meta_json": "{}",
                }
            ],
            mode="overwrite",
        )
        init_id = "init"
        self._table.delete(f"event_id = {init_id!r}")

    def upsert(self, record: VectorRecord) -> None:
        self._ensure_table()
        assert self._table is not None
        self._table.delete(f"event_id = {record.event_id!r}")
        self._table.add(
            [
                {
                    "event_id": record.event_id,
                    "vector": record.vector,
                    "content": record.content,
                    "source_tool": record.source_tool,
                    "event_type": record.event_type,
                    "session_id": record.session_id,
                    "meta_json": record.meta_json,
                }
            ]
        )



def build_vector_store(db_path: Path, backend: str, lancedb_uri: str | None = None) -> BaseVectorStore:
    if backend == "lancedb":
        uri = lancedb_uri or str(db_path.parent / "lancedb")
        return LanceDBVectorStore(uri=uri)
    return SQLiteVectorStore(db_path=db_path)
