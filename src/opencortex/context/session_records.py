# SPDX-License-Identifier: Apache-2.0
"""Session-scoped record queries for the conversation pipeline.

Wraps the per-session storage filters that ``ContextManager`` previously
exposed as private helpers (``_load_session_merged_records``,
``_load_session_directory_records``, ``_session_layer_counts``, plus the
inline session-summary lookup). Callers go through
``SessionRecordsRepository`` instead of touching the storage adapter
directly so that filter shape, sort discipline, and (eventually) scope
+ paging stay consistent across both the production conversation
lifecycle and the benchmark ingest service.

This module also re-homes two pure record-reading utilities,
``record_msg_range`` and ``record_text``, that the legacy helpers
relied on. They were previously ``@staticmethod`` on ``ContextManager``
but have no class state and are needed by the repository's sort logic;
moving them here avoids a circular import.

REVIEW context for this extraction:
- §25 Phase 5 (Repository / Gateway) of
  ``.context/compound-engineering/ce-code-review/20260424-152926-6301c860/REVIEW.md``
- Closure tracker entries PE-2 (silent 10000-row truncation), R3-RC-03
  (``source_uri=None`` degrades to session-wide scan), PE-6 (cross-tenant
  ``session_id`` collision footgun).

This U1 lands the mechanical extraction with behavior parity. U2 adds
the ``(tenant_id, user_id, source_uri)`` scope discipline + cursor
pagination + overflow guard.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple


def record_msg_range(record: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """Extract one normalized inclusive ``msg_range`` from a record payload."""
    meta = dict(record.get("meta") or {})
    raw_range = meta.get("msg_range", record.get("msg_range"))
    if not isinstance(raw_range, list) or len(raw_range) != 2:
        msg_index = meta.get("msg_index")
        try:
            index = int(msg_index)
        except (TypeError, ValueError):
            return None
        return index, index
    try:
        start = int(raw_range[0])
        end = int(raw_range[1])
    except (TypeError, ValueError):
        return None
    if start > end:
        return None
    return start, end


def record_text(record: Dict[str, Any]) -> str:
    """Choose the best available record text for recomposition input."""
    for key in ("content", "overview", "abstract"):
        value = str(record.get(key, "") or "").strip()
        if value:
            return value
    return ""


class SessionRecordsRepository:
    """Read-side gateway for session-scoped record queries.

    Constructed once per ``ContextManager`` instance with a storage
    adapter and a callable that resolves the active collection name
    (so the repo doesn't capture the whole orchestrator). All methods
    are async; storage adapters are async by contract.

    The U1 implementation preserves the legacy helper behavior
    bit-for-bit — same filter shape, same in-memory layer-filter, same
    msg-range sort. U2 will tighten the filter side to push
    ``(tenant_id, user_id, source_uri)`` into the storage filter and
    replace the ``limit=10000`` ceiling with cursor-based pagination.
    """

    def __init__(
        self,
        storage: Any,
        collection_resolver: Callable[[], str],
    ) -> None:
        self._storage = storage
        self._collection = collection_resolver

    async def load_merged(
        self,
        *,
        session_id: str,
        source_uri: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Load merged conversation leaves for one session in msg-range order."""
        records = await self._storage.filter(
            self._collection(),
            {
                "op": "and",
                "conds": [
                    {"op": "must", "field": "session_id", "conds": [session_id]},
                ],
            },
            limit=10000,
        )
        sortable: List[Tuple[int, int, Dict[str, Any]]] = []
        for record in records:
            meta = dict(record.get("meta") or {})
            if str(meta.get("layer", "") or "") != "merged":
                continue
            if source_uri:
                if str(meta.get("source_uri", "") or "") != source_uri:
                    continue
            msg_range = record_msg_range(record)
            if msg_range is None:
                continue
            sortable.append((msg_range[0], msg_range[1], record))
        sortable.sort(key=lambda item: (item[0], item[1]))
        return [record for _, _, record in sortable]

    async def load_directories(
        self,
        *,
        session_id: str,
        source_uri: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Load directory parent records for one session in msg-range order."""
        records = await self._storage.filter(
            self._collection(),
            {
                "op": "and",
                "conds": [
                    {"op": "must", "field": "session_id", "conds": [session_id]},
                ],
            },
            limit=10000,
        )
        sortable: List[Tuple[int, int, Dict[str, Any]]] = []
        for record in records:
            meta = dict(record.get("meta") or {})
            if str(meta.get("layer", "") or "") != "directory":
                continue
            if source_uri:
                if str(meta.get("source_uri", "") or "") != source_uri:
                    continue
            msg_range = record_msg_range(record)
            if msg_range is None:
                continue
            sortable.append((msg_range[0], msg_range[1], record))
        sortable.sort(key=lambda item: (item[0], item[1]))
        return [record for _, _, record in sortable]

    async def layer_counts(self, session_id: str) -> Dict[str, int]:
        """Return per-layer record counts for one session."""
        records = await self._storage.filter(
            self._collection(),
            {
                "op": "must",
                "field": "session_id",
                "conds": [session_id],
            },
            limit=10000,
        )
        counts: Dict[str, int] = {}
        for record in records:
            meta = dict(record.get("meta") or {})
            layer = str(meta.get("layer", "") or "<none>")
            counts[layer] = counts.get(layer, 0) + 1
        return counts

    async def load_summary(self, summary_uri: str) -> Optional[Dict[str, Any]]:
        """Fetch the session_summary record at ``summary_uri`` if present.

        Thin wrapper around the orchestrator's URI lookup. The benchmark
        idempotent-hit path uses this to decide whether to surface the
        prior run's ``summary_uri`` in the response. Returns ``None``
        when no such record exists.
        """
        records = await self._storage.filter(
            self._collection(),
            {"op": "must", "field": "uri", "conds": [summary_uri]},
            limit=1,
        )
        if not records:
            return None
        return records[0]
