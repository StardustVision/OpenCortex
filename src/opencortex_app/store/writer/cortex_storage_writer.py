# SPDX-License-Identifier: Apache-2.0
"""Writer for CFS-backed CortexStorage projections."""

from __future__ import annotations

from typing import Any

from opencortex_app.store.event.events import MemoryEvent
from opencortex_app.store.writer.event_payload import (
    event_content,
    event_uri,
    primary_record,
    record_abstract_json,
)


class CortexStorageWriter:
    """Write primary record layers to CortexStorage."""

    def __init__(self, *, cortex_storage: Any) -> None:
        self.cortex_storage = cortex_storage

    async def write(self, event: MemoryEvent) -> None:
        """Write L0, L1, and L2 files for one primary-record event."""
        if self.cortex_storage is None:
            return
        record = primary_record(event)
        if not record or not bool(record.get("retrieval_ready", False)):
            return
        await self.cortex_storage.write_context(
            uri=event_uri(event),
            content=event_content(event),
            abstract=str(record.get("abstract", "") or ""),
            abstract_json=record_abstract_json(record),
            overview=str(record.get("overview", "") or ""),
            is_leaf=bool(record.get("is_leaf", False)),
        )
