# SPDX-License-Identifier: Apache-2.0
"""Typed shape for entries flowing through anchor-clustered recomposition.

Defined in its own module so ``ContextManager`` and consumers can import
the type without pulling each other into a circular import. The shape
is enforced at construction time across three sites in ``manager.py``:

- ``ContextManager._benchmark_recomposition_entries`` — message-level
  entries from the benchmark offline ingest path. **Sets**
  ``source_segment_index`` to the input-segment index the entry came
  from (R3-RC-02 fix); the splitter uses this to force a hard split at
  input-segment boundaries.
- ``ContextManager._build_recomposition_entries`` — production
  conversation lifecycle entries. **Sets** ``source_segment_index`` to
  ``None``: live messages stream from ``Observer`` with no input-segment
  notion, so the boundary check is a strict no-op for this path.
- The inline builder inside ``ContextManager._run_full_session_recomposition``
  that re-derives entries from already-stored merged records. **Sets**
  ``source_segment_index`` to ``None`` for the same reason.

Keeping a single ``RecompositionEntry`` TypedDict instead of bare
``Dict[str, Any]`` lets consumers (``_build_anchor_clustered_segments``,
``_finalize_recomposition_segment``) annotate against a stable shape.
Drift across the three construction sites surfaces as a type error
the next time mypy / pyright runs, instead of as a silent runtime gap.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, TypedDict


class RecompositionEntry(TypedDict):
    """Single entry consumed by anchor-clustered recomposition.

    Every field is set unconditionally at all three construction sites
    today. ``immediate_uris`` and ``superseded_merged_uris`` are
    populated for production conversation entries and stay as empty
    lists for benchmark and re-derived entries — the lists are
    structurally required even when empty so downstream consumers
    never need to ``.get`` them with a default.

    ``source_segment_index`` (REVIEW closure tracker R3-RC-02) carries
    the input-segment index for benchmark entries and ``None`` for
    production-lifecycle / re-derived entries. The benchmark splitter
    forces a hard split when adjacent entries' indices differ; the
    ``None`` sentinel makes the check a strict no-op for non-benchmark
    paths so the production behavior is preserved.
    """

    text: str
    uri: str
    msg_start: int
    msg_end: int
    token_count: int
    anchor_terms: Set[str]
    time_refs: Set[str]
    source_record: Dict[str, Any]
    immediate_uris: List[str]
    superseded_merged_uris: List[str]
    source_segment_index: Optional[int]
