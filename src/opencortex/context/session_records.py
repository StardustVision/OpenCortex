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

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from opencortex.storage.storage_interface import StorageInterface

logger = logging.getLogger(__name__)


# Default page size when paging through filter results. Picked to keep
# round-trip count low on typical conversation sessions (~50 records)
# while staying well below any single-page memory pressure.
_DEFAULT_PAGE_SIZE = 1_000

# Safety stop on the pagination loop. 50 pages × 1 000 rows = 50 000
# records — orders of magnitude above any realistic single-session
# benchmark or production conversation. Hitting this almost certainly
# means a runaway query (cross-tenant session_id collision in storage,
# session_id payload corruption, or a session that should be rotated).
_DEFAULT_MAX_PAGES = 50


class SessionRecordOverflowError(Exception):
    """Raised when a session-scoped query exceeds the safety stop.

    Carries enough context to debug the source: the session_id under
    query, the running count when the stop fired, and the next cursor
    from the storage adapter (so an operator can resume the scroll
    manually if they need the full result set).

    The HTTP admin layer maps this to 507 Insufficient Storage with a
    structured detail payload. Production lifecycle callers can catch
    it themselves to decide between paging through or surfacing a
    diagnostic.
    """

    def __init__(
        self,
        *,
        session_id: str,
        count_at_stop: int,
        next_cursor: Optional[str],
        method: str,
    ) -> None:
        super().__init__(
            f"SessionRecordsRepository.{method}(session_id={session_id!r}) "
            f"exceeded the {_DEFAULT_MAX_PAGES}-page safety stop after "
            f"{count_at_stop} records (next_cursor={next_cursor!r}). "
            "This usually indicates a session_id payload anomaly or a "
            "cross-tenant collision; rotate the session_id or audit "
            "the storage payload before retrying."
        )
        self.session_id = session_id
        self.count_at_stop = count_at_stop
        self.next_cursor = next_cursor
        self.method = method


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
        storage: "StorageInterface",
        collection_resolver: Callable[[], str],
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
        max_pages: int = _DEFAULT_MAX_PAGES,
    ) -> None:
        self._storage = storage
        self._collection = collection_resolver
        self._page_size = page_size
        self._max_pages = max_pages

    def _build_session_filter(
        self,
        *,
        session_id: str,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Compose the storage filter dict for a session-scoped query.

        ``session_id`` is always required. ``tenant_id`` / ``user_id``
        are optional and pushed into the storage filter as additional
        ``must`` conditions when provided — this closes the cross-tenant
        ``session_id`` collision footgun (REVIEW PE-6) for callers that
        have identity available. The relevant indexed payload field
        names on the context collection are ``source_tenant_id`` /
        ``source_user_id`` (see ``storage/collection_schemas.py``).

        Callers that do not have identity (legacy or maintenance paths)
        can still pass ``tenant_id=None`` / ``user_id=None`` to preserve
        the U1 behavior — the filter simply omits those conds.
        """
        conds: List[Dict[str, Any]] = [
            {"op": "must", "field": "session_id", "conds": [session_id]},
        ]
        if tenant_id:
            conds.append(
                {"op": "must", "field": "source_tenant_id", "conds": [tenant_id]}
            )
        if user_id:
            conds.append(
                {"op": "must", "field": "source_user_id", "conds": [user_id]}
            )
        return {"op": "and", "conds": conds}

    async def _scroll_all(
        self,
        *,
        session_id: str,
        where: Dict[str, Any],
        method: str,
    ) -> List[Dict[str, Any]]:
        """Page through ``storage.scroll`` with the safety stop guard.

        Replaces the legacy ``limit=10_000`` silent truncation. Loops
        until the scroll cursor is exhausted OR ``_max_pages`` pages
        have been read; the latter raises ``SessionRecordOverflowError``
        with the cursor + count so the caller can decide whether to
        keep paging or surface a diagnostic. Page size is configurable
        per repo instance (constructor kwarg).

        Falls back to ``storage.filter`` with ``limit=page_size *
        max_pages`` when the storage adapter does not support scroll
        (in-memory test fixtures, for example) — this preserves
        single-call semantics for those backends while still providing
        the overflow signal.
        """
        all_records: List[Dict[str, Any]] = []
        scroll = getattr(self._storage, "scroll", None)
        if scroll is None or not callable(scroll):
            # Fallback for storage adapters without scroll: one filter
            # call with a page-size * max-pages limit. Request limit+1
            # so the overflow guard fires only when the result set
            # genuinely exceeds the cap (REVIEW correctness-002 /
            # ADV-U-002 — equality at the cap was a false-positive
            # boundary).
            cap = self._page_size * self._max_pages
            page = await self._storage.filter(
                self._collection(),
                where,
                limit=cap + 1,
            )
            if len(page) > cap:
                raise SessionRecordOverflowError(
                    session_id=session_id,
                    count_at_stop=len(page),
                    next_cursor=None,
                    method=method,
                )
            return list(page)

        cursor: Optional[str] = None
        for _ in range(self._max_pages):
            page, cursor = await scroll(
                self._collection(),
                filter=where,
                limit=self._page_size,
                cursor=cursor,
            )
            all_records.extend(page)
            if cursor is None:
                return all_records
        # Hit the safety stop — the next cursor is non-None so there
        # are more records out there. Surface as overflow.
        raise SessionRecordOverflowError(
            session_id=session_id,
            count_at_stop=len(all_records),
            next_cursor=cursor,
            method=method,
        )

    async def load_merged(
        self,
        *,
        session_id: str,
        source_uri: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Load merged conversation leaves for one session in msg-range order.

        ``tenant_id`` / ``user_id`` (optional) push the cross-tenant
        scope into the storage filter — see ``_build_session_filter``.
        ``source_uri`` (optional) is filtered in-memory after fetch
        because it lives on ``meta.source_uri`` (not a top-level
        indexed field on the context collection).
        """
        where = self._build_session_filter(
            session_id=session_id, tenant_id=tenant_id, user_id=user_id
        )
        records = await self._scroll_all(
            session_id=session_id, where=where, method="load_merged"
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
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Load directory parent records for one session in msg-range order."""
        where = self._build_session_filter(
            session_id=session_id, tenant_id=tenant_id, user_id=user_id
        )
        records = await self._scroll_all(
            session_id=session_id, where=where, method="load_directories"
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

    async def layer_counts(
        self,
        session_id: str,
        *,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """Return per-layer record counts for one session."""
        where = self._build_session_filter(
            session_id=session_id, tenant_id=tenant_id, user_id=user_id
        )
        records = await self._scroll_all(
            session_id=session_id, where=where, method="layer_counts"
        )
        counts: Dict[str, int] = {}
        for record in records:
            meta = dict(record.get("meta") or {})
            layer = str(meta.get("layer", "") or "<none>")
            counts[layer] = counts.get(layer, 0) + 1
        return counts

    async def load_summary(self, summary_uri: str) -> Optional[Dict[str, Any]]:
        """Fetch the session_summary record at ``summary_uri`` if present.

        Returns ``None`` when no such record exists. URI lookup is
        always exactly-one-record, so this method does not paginate
        and no overflow guard applies.
        """
        records = await self._storage.filter(
            self._collection(),
            {"op": "must", "field": "uri", "conds": [summary_uri]},
            limit=1,
        )
        if not records:
            return None
        return records[0]
