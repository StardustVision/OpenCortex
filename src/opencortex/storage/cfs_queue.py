# SPDX-License-Identifier: Apache-2.0
"""SQLite-backed persistent queues stored under CFS."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from opencortex.storage.cfs import CFS


class QueueMessage(BaseModel):
    """One dequeued persistent queue message."""

    id: str
    queue_name: str
    payload: dict[str, Any]
    attempts: int = 0
    max_attempts: int = 3


class QueueStatus(BaseModel):
    """Persistent queue status counters."""

    pending: int = 0
    processing: int = 0
    done: int = 0
    failed: int = 0
    requeue_count: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)


class CFSQueue:
    """Small SQLite queue with OpenViking-style enqueue/dequeue/ack semantics."""

    def __init__(
        self,
        *,
        cfs: CFS,
        relative_path: str | Path = "_system/queue/queue.db",
        stale_after_seconds: float = 300.0,
    ) -> None:
        self.cfs = cfs
        self.db_path = self.cfs.resolve(relative_path)
        self.stale_after_seconds = max(1.0, float(stale_after_seconds))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def enqueue(
        self,
        queue_name: str,
        payload: dict[str, Any],
        *,
        max_attempts: int = 3,
    ) -> str:
        """Persist one pending queue message."""
        message_id = uuid4().hex
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO queue_messages (
                    id, queue_name, payload, status, attempts, max_attempts,
                    available_at, created_at, updated_at
                )
                VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    queue_name,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    max(1, int(max_attempts)),
                    now,
                    now,
                    now,
                ),
            )
        return message_id

    async def aenqueue(
        self,
        queue_name: str,
        payload: dict[str, Any],
        *,
        max_attempts: int = 3,
    ) -> str:
        """Persist one pending queue message without blocking the event loop."""
        return await asyncio.to_thread(
            self.enqueue,
            queue_name,
            payload,
            max_attempts=max_attempts,
        )

    def dequeue(self, queue_name: str) -> QueueMessage | None:
        """Atomically claim one pending message for processing."""
        now = time.time()
        self.recover_stale(queue_name, now=now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, queue_name, payload, attempts, max_attempts
                FROM queue_messages
                WHERE queue_name = ?
                  AND status = 'pending'
                  AND available_at <= ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (queue_name, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """
                UPDATE queue_messages
                SET status = 'processing',
                    attempts = attempts + 1,
                    processing_started_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, row["id"]),
            )
            conn.commit()
            return QueueMessage(
                id=str(row["id"]),
                queue_name=str(row["queue_name"]),
                payload=json.loads(str(row["payload"] or "{}")),
                attempts=int(row["attempts"] or 0) + 1,
                max_attempts=int(row["max_attempts"] or 1),
            )

    async def adequeue(self, queue_name: str) -> QueueMessage | None:
        """Claim one pending message without blocking the event loop."""
        return await asyncio.to_thread(self.dequeue, queue_name)

    def ack(self, message_id: str) -> None:
        """Mark one processing message as done."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE queue_messages
                SET status = 'done',
                    processing_started_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, message_id),
            )

    async def aack(self, message_id: str) -> None:
        """Mark one processing message as done without blocking the event loop."""
        await asyncio.to_thread(self.ack, message_id)

    def release(self, message_id: str, *, delay_seconds: float = 0.05) -> None:
        """Return one processing message to pending without consuming a retry."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE queue_messages
                SET status = 'pending',
                    processing_started_at = NULL,
                    attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                    available_at = ?,
                    updated_at = ?
                WHERE id = ?
                  AND status = 'processing'
                """,
                (now + max(0.0, delay_seconds), now, message_id),
            )

    async def arelease(self, message_id: str, *, delay_seconds: float = 0.05) -> None:
        """Return one processing message to pending without blocking the loop."""
        await asyncio.to_thread(self.release, message_id, delay_seconds=delay_seconds)

    def fail(
        self,
        message_id: str,
        error: str,
        *,
        retry: bool,
        delay_seconds: float = 1.0,
    ) -> None:
        """Record a processing failure and optionally requeue the message."""
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM queue_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"] or 0)
            max_attempts = int(row["max_attempts"] or 1)
            should_retry = retry and attempts < max_attempts
            if should_retry:
                conn.execute(
                    """
                    UPDATE queue_messages
                    SET status = 'pending',
                        processing_started_at = NULL,
                        available_at = ?,
                        requeue_count = requeue_count + 1,
                        last_error = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now + max(0.0, delay_seconds), error, now, message_id),
                )
                return
            conn.execute(
                """
                UPDATE queue_messages
                SET status = 'failed',
                    processing_started_at = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error, now, message_id),
            )

    async def afail(
        self,
        message_id: str,
        error: str,
        *,
        retry: bool,
        delay_seconds: float = 1.0,
    ) -> None:
        """Record failure without blocking the event loop."""
        await asyncio.to_thread(
            self.fail,
            message_id,
            error,
            retry=retry,
            delay_seconds=delay_seconds,
        )

    def recover_stale(
        self,
        queue_name: str | None = None,
        *,
        now: float | None = None,
    ) -> int:
        """Move stale processing messages back to pending."""
        timestamp = time.time() if now is None else now
        cutoff = timestamp - self.stale_after_seconds
        sql = """
            UPDATE queue_messages
            SET status = 'pending',
                processing_started_at = NULL,
                available_at = ?,
                requeue_count = requeue_count + 1,
                updated_at = ?
            WHERE status = 'processing'
              AND processing_started_at IS NOT NULL
              AND processing_started_at < ?
        """
        params: tuple[Any, ...]
        if queue_name:
            sql += " AND queue_name = ?"
            params = (timestamp, timestamp, cutoff, queue_name)
        else:
            params = (timestamp, timestamp, cutoff)
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            return int(cursor.rowcount or 0)

    def requeue_failed(
        self,
        queue_name: str,
        *,
        max_attempts: int | None = None,
        reset_attempts: bool = True,
    ) -> int:
        """Move failed messages back to pending for an explicit retry pass."""
        now = time.time()
        attempts_sql = "0" if reset_attempts else "attempts"
        max_attempts_sql = (
            "max_attempts" if max_attempts is None else str(max(1, int(max_attempts)))
        )
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE queue_messages
                SET status = 'pending',
                    attempts = {attempts_sql},
                    max_attempts = {max_attempts_sql},
                    processing_started_at = NULL,
                    available_at = ?,
                    requeue_count = requeue_count + 1,
                    updated_at = ?
                WHERE queue_name = ?
                  AND status = 'failed'
                """,
                (now, now, queue_name),
            )
            return int(cursor.rowcount or 0)

    def status(self, queue_name: str) -> QueueStatus:
        """Return queue status counters."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM queue_messages
                WHERE queue_name = ?
                GROUP BY status
                """,
                (queue_name,),
            ).fetchall()
            errors = conn.execute(
                """
                SELECT id, attempts, max_attempts, last_error, updated_at
                FROM queue_messages
                WHERE queue_name = ?
                  AND status = 'failed'
                ORDER BY updated_at DESC
                LIMIT 100
                """,
                (queue_name,),
            ).fetchall()
            requeues = conn.execute(
                """
                SELECT COALESCE(SUM(requeue_count), 0) AS total
                FROM queue_messages
                WHERE queue_name = ?
                """,
                (queue_name,),
            ).fetchone()
        counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
        requeue_count = int(requeues["total"] or 0) if requeues else 0
        return QueueStatus(
            pending=counts.get("pending", 0),
            processing=counts.get("processing", 0),
            done=counts.get("done", 0),
            failed=counts.get("failed", 0),
            requeue_count=requeue_count,
            errors=[
                {
                    "id": row["id"],
                    "attempts": row["attempts"],
                    "max_attempts": row["max_attempts"],
                    "message": row["last_error"],
                    "updated_at": row["updated_at"],
                }
                for row in errors
            ],
        )

    async def astatus(self, queue_name: str) -> QueueStatus:
        """Return queue status counters without blocking the event loop."""
        return await asyncio.to_thread(self.status, queue_name)

    def clear(self, queue_name: str | None = None) -> None:
        """Delete queue rows, primarily for tests."""
        with self._connect() as conn:
            if queue_name:
                conn.execute(
                    "DELETE FROM queue_messages WHERE queue_name = ?",
                    (queue_name,),
                )
            else:
                conn.execute("DELETE FROM queue_messages")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_messages (
                    id TEXT PRIMARY KEY,
                    queue_name TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at REAL NOT NULL,
                    processing_started_at REAL,
                    requeue_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_queue_messages_claim
                ON queue_messages(queue_name, status, available_at, created_at)
                """
            )
