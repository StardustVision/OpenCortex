# SPDX-License-Identifier: Apache-2.0
"""Shared mapping helpers for conversation-style benchmark adapters.

`LongMemEvalBench` (`benchmarks/adapters/conversation.py`) and
`LoCoMoBench` (`benchmarks/adapters/locomo.py`) both turn benchmark
ingest responses into per-session URI maps that drive QA-time recall.
The transformation logic is pure — no instance state — and was
duplicated across the two adapters: ~169 lines, 10.83% jscpd
(REVIEW closure tracker §25 Phase 7 / R2-24 / R4-P2-8 / R4-P2-9).

This module is the single home for that logic. Any helper used by ≥2
conversation-style adapters lives here, not on the adapter class.

Naming convention follows ``benchmarks/adapters/base.py``: public
symbols (no leading underscore) since the module is a public API for
its sibling modules.

Out of scope on purpose:
- ``benchmarks/adapters/beam.py`` — only shares the ingest call shape,
  not these helpers. Beam can adopt ``extract_records_by_uri`` in a
  follow-up PR once the helper proves stable.
- ``LongMemEvalBench._lme_session_to_uri`` — LME-specific haystack
  mapping with open non-determinism (REVIEW F3 / ADV-003). Stays on
  the adapter side; pulling it here would falsely advertise it as a
  pattern both adapters use.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


def normalize_text_set(values: Iterable[Any]) -> Set[str]:
    """Normalize heterogeneous string values for exact-set matching."""
    normalized: Set[str] = set()
    for value in values:
        text = str(value or "").strip().lower()
        if text:
            normalized.add(text)
    return normalized


def message_span(record: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    """Return a record's message span from the top-level ``msg_range`` contract."""
    raw_range = record.get("msg_range")
    if not isinstance(raw_range, list) or len(raw_range) != 2:
        return None
    try:
        start = int(raw_range[0])
        end = int(raw_range[1])
    except (TypeError, ValueError):
        return None
    if start > end:
        return None
    return start, end


def ranges_overlap(left: Tuple[int, int], right: Tuple[int, int]) -> bool:
    """Return whether two inclusive ranges overlap."""
    return max(left[0], right[0]) <= min(left[1], right[1])


def overlap_width(left: Tuple[int, int], right: Tuple[int, int]) -> int:
    """Return inclusive overlap width for two spans."""
    if not ranges_overlap(left, right):
        return 0
    return min(left[1], right[1]) - max(left[0], right[0]) + 1


def record_time_refs(record: Dict[str, Any]) -> Set[str]:
    """Extract normalized temporal anchors from a memory list payload."""
    values: List[Any] = []
    meta = record.get("meta")
    if isinstance(meta, dict):
        values.extend(meta.get("time_refs") or [])
        values.append(meta.get("event_date"))

    abstract_json = record.get("abstract_json")
    if isinstance(abstract_json, dict):
        slots = abstract_json.get("slots")
        if isinstance(slots, dict):
            values.extend(slots.get("time_refs") or [])

    values.append(record.get("event_date"))
    return normalize_text_set(values)


async def memory_record_snapshot(oc: Any) -> Dict[str, Dict[str, Any]]:
    """Snapshot current memory records for diff-based ground-truth mapping.

    Pages through ``oc.memory_list(context_type="memory", category="events")``
    in 500-record batches until exhausted. Used by both adapters' mcp-path
    branch to capture the before/after record set so the new ingest's
    URIs can be derived by set difference.
    """
    offset = 0
    limit = 500
    records_by_uri: Dict[str, Dict[str, Any]] = {}
    while True:
        payload = await oc.memory_list(
            context_type="memory",
            category="events",
            limit=limit,
            offset=offset,
            include_payload=True,
        )
        results = payload.get("results", [])
        for item in results:
            uri = str(item.get("uri", "") or "")
            if uri:
                records_by_uri[uri] = dict(item)
        if len(results) < limit:
            break
        offset += limit
    return records_by_uri


def map_session_uris(
    *,
    session_spans: Dict[int, Tuple[int, int]],
    session_time_refs: Dict[int, Set[str]],
    records_by_uri: Dict[str, Dict[str, Any]],
    conversation_session_id: str,
    return_all: bool = False,
) -> Dict[int, List[str]]:
    """Map inner-conversation sessions to merged-leaf URIs.

    Two-pass mapping over ``records_by_uri``:

    1. **Span-based**: every record with a valid ``msg_range`` that
       overlaps a session's span contributes one candidate per session
       it overlaps. Candidates are sorted ``(-overlap_width, width,
       span_start, uri_lex)`` so the tightest (highest overlap, smallest
       record width, earliest start, lex-first uri on ties) ranks first.
    2. **time_refs fallback**: for sessions still empty after step 1, a
       record without ``msg_range`` whose ``record_time_refs`` intersects
       the session's ``time_refs`` becomes a low-priority candidate
       (``overlap_width=0``, ``width=10**9``).

    The shared structure was previously duplicated in
    ``conversation.py`` and ``locomo.py``; only the return shape
    diverged:

    - ``return_all=False`` (default — matches the legacy
      ``LongMemEvalBench._map_session_uris`` contract): returns a
      single-element list ``[best_uri]`` per session — the tightest
      candidate after sort.
    - ``return_all=True`` (matches the legacy ``LoCoMoBench`` contract):
      returns the full sorted candidate list per session, deferring
      tie-break to the caller (``LoCoMoBench._select_best_session_uri``
      applies a question-aware lexical refinement on the head of the
      list).

    Sessions with zero candidates always map to ``[]``.
    """
    relevant_records: Dict[str, Dict[str, Any]] = {}
    for uri, record in records_by_uri.items():
        record_session_id = str(record.get("session_id", "") or "")
        if record_session_id and record_session_id != conversation_session_id:
            continue
        relevant_records[uri] = record

    mapped: Dict[int, List[Tuple[str, int, int, int]]] = {
        session_num: [] for session_num in session_spans
    }
    unmatched_records: Dict[str, Dict[str, Any]] = {}

    for uri, record in relevant_records.items():
        span = message_span(record)
        if span is None:
            unmatched_records[uri] = record
            continue
        width = span[1] - span[0]
        for session_num, session_span in session_spans.items():
            if not ranges_overlap(span, session_span):
                continue
            mapped[session_num].append(
                (uri, width, overlap_width(span, session_span), span[0])
            )

    if unmatched_records:
        for session_num, time_refs in session_time_refs.items():
            if mapped[session_num] or not time_refs:
                continue
            for uri, record in unmatched_records.items():
                if time_refs.intersection(record_time_refs(record)):
                    span = message_span(record)
                    width = span[1] - span[0] if span is not None else 10**9
                    mapped[session_num].append(
                        (uri, width, 0, span[0] if span else 10**9)
                    )

    result: Dict[int, List[str]] = {}
    for session_num, candidates in mapped.items():
        if not candidates:
            result[session_num] = []
            continue
        ordered = sorted(
            candidates,
            key=lambda item: (-item[2], item[1], item[3], item[0]),
        )
        if return_all:
            result[session_num] = [uri for uri, _, _, _ in ordered]
        else:
            result[session_num] = [ordered[0][0]]
    return result


def extract_records_by_uri(
    payload: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Drain ``payload["records"]`` into the canonical ``{uri: dict(record)}``.

    Replaces the inline comprehension that appears in every store-path and
    mainstream-path response handler in both adapters. Pure mapping —
    does not bake in any ``ingest_shape`` assumption (callers vary on
    that knob); just normalizes the response into a URI-keyed dict.

    Records with empty / missing / non-string ``uri`` are filtered out.
    The returned dicts are shallow copies so callers can mutate them
    without aliasing the input payload.
    """
    return {
        str(record.get("uri", "") or ""): dict(record)
        for record in payload.get("records", []) or []
        if str(record.get("uri", "") or "")
    }
