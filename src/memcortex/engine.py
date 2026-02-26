from __future__ import annotations

from dataclasses import asdict
import json
import time
import uuid

from .config import MemCortexConfig
from .embeddings import deterministic_embedding
from .lock import file_lock
from .spool import SpoolStats, SpoolStore
from .sync_client import MockMcpSyncClient
from .vector_store import VectorRecord, build_vector_store



def should_flush(stats: SpoolStats, cfg: MemCortexConfig) -> bool:
    return (
        stats.backlog_count >= cfg.count_threshold
        or stats.oldest_age_seconds >= cfg.age_threshold_seconds
        or stats.db_bytes >= cfg.spool_max_bytes
    )



def maybe_flush(cfg: MemCortexConfig, local_only: bool = False) -> dict:
    spool = SpoolStore(cfg.db_path)
    stats = spool.stats()
    if not should_flush(stats, cfg):
        return {
            "flushed": False,
            "reason": "threshold_not_met",
            "stats": asdict(stats),
        }
    flush_result = flush(cfg=cfg, local_only=local_only)
    return {
        "flushed": True,
        "reason": "threshold_met",
        "stats": asdict(stats),
        "flush": flush_result,
    }



def flush(cfg: MemCortexConfig, local_only: bool = False, force: bool = False) -> dict:
    start = time.time()
    processed = 0
    failed = 0

    spool = SpoolStore(cfg.db_path)
    store = build_vector_store(
        db_path=cfg.db_path, backend=cfg.vector_backend, lancedb_uri=cfg.lancedb_uri
    )

    with file_lock(cfg.lock_path):
        if not force:
            current_stats = spool.stats()
            if not should_flush(current_stats, cfg):
                return {
                    "processed": 0,
                    "failed": 0,
                    "synced": 0,
                    "reason": "threshold_not_met",
                    "stats": asdict(current_stats),
                }

        rows = spool.reserve_events(
            batch_size=cfg.reserve_batch_size,
            lease_timeout_seconds=cfg.lease_timeout_seconds,
        )
        for row in rows:
            elapsed_ms = int((time.time() - start) * 1000)
            if elapsed_ms >= cfg.flush_time_budget_ms:
                break

            try:
                vector = deterministic_embedding(row["content"])
                record = VectorRecord(
                    event_id=row["event_id"],
                    content=row["content"],
                    vector=vector,
                    source_tool=row["source_tool"],
                    event_type=row["event_type"],
                    session_id=row["session_id"],
                    meta_json=row["meta_json"],
                )
                store.upsert(record)
                spool.mark_processed(row["event_id"])
                processed += 1
            except Exception as exc:  # pragma: no cover
                failed += 1
                spool.mark_failed(
                    event_id=row["event_id"],
                    error_message=str(exc),
                    max_retries=cfg.max_retries,
                )

        synced = 0
        final_stats = spool.stats()
        if not local_only and final_stats.backlog_count <= cfg.backlog_low_watermark:
            synced = sync_pending(cfg, limit=cfg.reserve_batch_size)

    return {
        "processed": processed,
        "failed": failed,
        "synced": synced,
        "stats": asdict(spool.stats()),
    }



def sync_pending(cfg: MemCortexConfig, limit: int = 50) -> int:
    spool = SpoolStore(cfg.db_path)
    client = MockMcpSyncClient(cfg.sync_outbox_path)

    pending = spool.pending_sync_events(limit=limit)
    if not pending:
        return 0

    batch_id = f"bat_{uuid.uuid4().hex[:12]}"
    events = []
    for row in pending:
        events.append(
            {
                "event_id": row["event_id"],
                "payload_hash": row["payload_hash"],
                "source_tool": row["source_tool"],
                "event_type": row["event_type"],
                "session_id": row["session_id"],
                "content_summary": summarize_content(row["content"]),
                "raw_content": row["content"],
                "meta": json.loads(row["meta_json"]),
                "occurred_at": row["created_at"],
            }
        )

    result = client.ingest_batch(batch_id=batch_id, events=events)
    synced_count = spool.mark_synced(result.accepted_ids + result.duplicate_ids)
    for rejected in result.rejected:
        spool.mark_sync_failed(
            event_id=rejected.get("event_id", ""),
            error_message=rejected.get("message", "unknown"),
            max_retries=cfg.max_retries,
        )
    return synced_count



def summarize_content(content: str, max_chars: int = 280) -> str:
    if len(content) <= max_chars:
        return content
    return content[: max_chars - 1] + "…"
