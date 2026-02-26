from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import time


@dataclass
class SyncResult:
    accepted_ids: list[str]
    duplicate_ids: list[str]
    rejected: list[dict[str, str]]


class MockMcpSyncClient:
    """Local mock client used by scaffold until real MCP transport is wired."""

    def __init__(self, outbox_path: Path):
        self.outbox_path = outbox_path
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)

    def ingest_batch(self, batch_id: str, events: list[dict]) -> SyncResult:
        payload = {
            "batch_id": batch_id,
            "events": events,
            "sent_at": time.time(),
        }
        with self.outbox_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")

        accepted_ids = [event["event_id"] for event in events]
        return SyncResult(accepted_ids=accepted_ids, duplicate_ids=[], rejected=[])
