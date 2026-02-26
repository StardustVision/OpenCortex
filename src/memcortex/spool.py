from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import sqlite3
import time
from typing import Iterable

from .models import MemoryEvent


@dataclass
class SpoolStats:
    backlog_count: int
    oldest_age_seconds: int
    pending_sync_count: int
    db_bytes: int


class SpoolStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    source_tool TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    meta_json TEXT NOT NULL,
                    domain_hint TEXT,
                    confidence REAL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL,
                    lease_until REAL,
                    last_error TEXT,
                    processed_at REAL,
                    sync_status TEXT NOT NULL DEFAULT 'pending'
                );

                CREATE INDEX IF NOT EXISTS idx_events_status_next_retry
                ON events(status, next_retry_at);

                CREATE INDEX IF NOT EXISTS idx_events_lease
                ON events(lease_until);

                CREATE INDEX IF NOT EXISTS idx_events_sync_status
                ON events(sync_status, status);

                CREATE TABLE IF NOT EXISTS dead_letters (
                    dlq_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    reason_message TEXT,
                    failed_at REAL NOT NULL
                );
                """
            )

    def add_event(self, event: MemoryEvent) -> str:
        now = time.time()
        event = event.normalize()
        payload_json = json.dumps(
            {
                "event_id": event.event_id,
                "source_tool": event.source_tool,
                "session_id": event.session_id,
                "event_type": event.event_type,
                "content": event.content,
                "meta": event.meta,
                "domain_hint": event.domain_hint,
                "confidence": event.confidence,
                "created_at": event.created_at,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        payload_hash = f"sha256:{hashlib.sha256(payload_json.encode('utf-8')).hexdigest()}"

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events (
                    event_id, created_at, updated_at, source_tool, session_id, event_type,
                    content, meta_json, domain_hint, confidence, payload_hash, status,
                    retry_count, next_retry_at, lease_until, last_error, processed_at,
                    sync_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 0, NULL, NULL, NULL, NULL, 'pending')
                """,
                (
                    event.event_id,
                    event.created_at,
                    now,
                    event.source_tool,
                    event.session_id,
                    event.event_type,
                    event.content,
                    json.dumps(event.meta, ensure_ascii=False),
                    event.domain_hint,
                    event.confidence,
                    payload_hash,
                ),
            )
        return payload_hash

    def stats(self) -> SpoolStats:
        with self._connect() as conn:
            now = time.time()
            backlog_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE status IN ('new','failed','reserved')"
            ).fetchone()[0]
            oldest_ts = conn.execute(
                "SELECT MIN(created_at) FROM events WHERE status IN ('new','failed','reserved')"
            ).fetchone()[0]
            pending_sync_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE status='processed' AND sync_status='pending'"
            ).fetchone()[0]

        oldest_age_seconds = int(max(0, now - oldest_ts)) if oldest_ts else 0
        db_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        wal_path = self.db_path.with_suffix(self.db_path.suffix + "-wal")
        if wal_path.exists():
            db_bytes += wal_path.stat().st_size
        return SpoolStats(
            backlog_count=backlog_count,
            oldest_age_seconds=oldest_age_seconds,
            pending_sync_count=pending_sync_count,
            db_bytes=db_bytes,
        )

    def requeue_expired_leases(self, now: float) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE events
                SET status='failed',
                    next_retry_at=?,
                    lease_until=NULL,
                    updated_at=?,
                    last_error='LEASE_EXPIRED'
                WHERE status='reserved' AND lease_until IS NOT NULL AND lease_until < ?
                """,
                (now, now, now),
            )
            return cur.rowcount

    def reserve_events(self, batch_size: int, lease_timeout_seconds: int) -> list[sqlite3.Row]:
        now = time.time()
        lease_until = now + lease_timeout_seconds
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE events
                SET status='new', lease_until=NULL, updated_at=?
                WHERE status='reserved' AND lease_until IS NOT NULL AND lease_until < ?
                """,
                (now, now),
            )
            event_ids = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT event_id
                    FROM events
                    WHERE (status='new' OR (status='failed' AND (next_retry_at IS NULL OR next_retry_at <= ?)))
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (now, batch_size),
                ).fetchall()
            ]
            if not event_ids:
                conn.execute("COMMIT")
                return []

            placeholders = ",".join("?" for _ in event_ids)
            conn.execute(
                f"""
                UPDATE events
                SET status='reserved', lease_until=?, updated_at=?
                WHERE event_id IN ({placeholders})
                """,
                (lease_until, now, *event_ids),
            )
            rows = conn.execute(
                f"SELECT * FROM events WHERE event_id IN ({placeholders}) ORDER BY created_at ASC",
                event_ids,
            ).fetchall()
            conn.execute("COMMIT")
            return rows

    def mark_processed(self, event_id: str) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE events
                SET status='processed', processed_at=?, lease_until=NULL,
                    updated_at=?, last_error=NULL
                WHERE event_id=?
                """,
                (now, now, event_id),
            )

    def mark_failed(self, event_id: str, error_message: str, max_retries: int) -> None:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT retry_count, payload_hash, source_tool, session_id, event_type, content, meta_json FROM events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if not row:
                return

            retry_count = int(row[0]) + 1
            if retry_count > max_retries:
                payload_json = json.dumps(
                    {
                        "event_id": event_id,
                        "source_tool": row[2],
                        "session_id": row[3],
                        "event_type": row[4],
                        "content": row[5],
                        "meta_json": row[6],
                    },
                    ensure_ascii=False,
                )
                conn.execute(
                    """
                    INSERT INTO dead_letters(
                        event_id, payload_hash, payload_json, reason_code, reason_message, failed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (event_id, row[1], payload_json, "MAX_RETRIES_EXCEEDED", error_message, now),
                )
                conn.execute("DELETE FROM events WHERE event_id=?", (event_id,))
                return

            delay = retry_delay_seconds(retry_count)
            next_retry = now + delay
            conn.execute(
                """
                UPDATE events
                SET status='failed', retry_count=?, next_retry_at=?, lease_until=NULL,
                    updated_at=?, last_error=?
                WHERE event_id=?
                """,
                (retry_count, next_retry, now, error_message[:500], event_id),
            )

    def pending_sync_events(self, limit: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM events
                WHERE status='processed' AND sync_status='pending'
                ORDER BY processed_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def mark_synced(self, event_ids: Iterable[str]) -> int:
        event_ids = list(event_ids)
        if not event_ids:
            return 0
        placeholders = ",".join("?" for _ in event_ids)
        with self._connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE events
                SET sync_status='synced', updated_at=?
                WHERE event_id IN ({placeholders})
                """,
                (time.time(), *event_ids),
            )
            return cur.rowcount

    def mark_sync_failed(self, event_id: str, error_message: str, max_retries: int) -> None:
        # Reuse the event retry channel for sync failures too.
        self.mark_failed(event_id=event_id, error_message=f"SYNC_FAILED:{error_message}", max_retries=max_retries)



def retry_delay_seconds(retry_count: int) -> int:
    schedule = {1: 10, 2: 30, 3: 120}
    return schedule.get(retry_count, 120)
