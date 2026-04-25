# SPDX-License-Identifier: Apache-2.0
"""Typed shape for entries flowing through anchor-clustered recomposition.

Defined in its own module so ``ContextManager`` and consumers can import
the type without pulling each other into a circular import. The shape
is enforced at construction time across three sites in ``manager.py``:

- ``ContextManager._benchmark_recomposition_entries`` — message-level
  entries from the benchmark offline ingest path.
- ``ContextManager._build_recomposition_entries`` — production
  conversation lifecycle entries.
- The inline builder inside ``ContextManager._run_full_session_recomposition``
  that re-derives entries from already-stored merged records.

Keeping a single ``RecompositionEntry`` TypedDict instead of bare
``Dict[str, Any]`` lets consumers (``_build_anchor_clustered_segments``,
``_finalize_recomposition_segment``) annotate against a stable shape.
Drift across the three construction sites surfaces as a type error
the next time mypy / pyright runs, instead of as a silent runtime gap.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set, TypedDict


class RecompositionEntry(TypedDict):
    """Single entry consumed by anchor-clustered recomposition.

    Every field is set unconditionally at all three construction sites
    today. ``immediate_uris`` and ``superseded_merged_uris`` are
    populated for production conversation entries and stay as empty
    lists for benchmark and re-derived entries — the lists are
    structurally required even when empty so downstream consumers
    never need to ``.get`` them with a default.
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
