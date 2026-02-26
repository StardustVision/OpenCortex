from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class MemCortexConfig:
    home: Path
    db_path: Path
    lock_path: Path
    sync_outbox_path: Path
    count_threshold: int = 20
    age_threshold_seconds: int = 180
    spool_max_bytes: int = 100 * 1024 * 1024
    compact_threshold_bytes: int = 70 * 1024 * 1024
    backlog_low_watermark: int = 10
    reserve_batch_size: int = 50
    flush_time_budget_ms: int = 800
    lease_timeout_seconds: int = 30
    max_retries: int = 3
    vector_backend: str = "sqlite"
    lancedb_uri: str | None = None



def load_config() -> MemCortexConfig:
    home = Path(os.environ.get("MEMCORTEX_HOME", Path.home() / ".memcortex"))
    spool_dir = home / "spool"
    state_dir = home / "state"
    spool_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    db_path = Path(os.environ.get("MEMCORTEX_DB_PATH", spool_dir / "events.db"))
    lock_path = Path(os.environ.get("MEMCORTEX_LOCK_PATH", state_dir / "flush.lock"))
    sync_outbox_path = Path(
        os.environ.get("MEMCORTEX_SYNC_OUTBOX_PATH", state_dir / "sync-outbox.jsonl")
    )

    vector_backend = os.environ.get("MEMCORTEX_VECTOR_BACKEND", "sqlite").strip().lower()
    lancedb_uri = os.environ.get("MEMCORTEX_LANCEDB_URI")

    return MemCortexConfig(
        home=home,
        db_path=db_path,
        lock_path=lock_path,
        sync_outbox_path=sync_outbox_path,
        vector_backend=vector_backend,
        lancedb_uri=lancedb_uri,
    )
