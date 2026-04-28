# SPDX-License-Identifier: Apache-2.0
"""Tests for pure recomposition segmentation algorithms."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from opencortex.context.recomposition_segmentation import (
    _RECOMPOSE_CLUSTER_MAX_TOKENS,
    RecompositionSegmentationService,
)
from opencortex.context.recomposition_types import RecompositionEntry


def _entry(
    msg_start: int,
    msg_end: int,
    *,
    text: str = "message",
    uri: str = "",
    anchors: Optional[List[str]] = None,
    time_refs: Optional[List[str]] = None,
    tokens: int = 10,
    source_segment_index: Optional[int] = None,
    immediate_uris: Optional[List[str]] = None,
    superseded_merged_uris: Optional[List[str]] = None,
) -> RecompositionEntry:
    normalized_uri = (
        uri
        or f"opencortex://tenant/user/memories/events/e-{msg_start:06d}-{msg_end:06d}"
    )
    return RecompositionEntry(
        text=text,
        uri=normalized_uri,
        msg_start=msg_start,
        msg_end=msg_end,
        token_count=tokens,
        anchor_terms=set(anchors or []),
        time_refs=set(time_refs or []),
        source_record={
            "uri": normalized_uri,
            "meta": {"msg_range": [msg_start, msg_end]},
        },
        immediate_uris=list(immediate_uris or []),
        superseded_merged_uris=list(superseded_merged_uris or []),
        source_segment_index=source_segment_index,
    )


def test_extracts_anchor_terms_from_record_meta_and_slots() -> None:
    """Anchor extraction merges entities and topic-like values."""
    service = RecompositionSegmentationService()
    record: Dict[str, Any] = {
        "entities": ["Alice"],
        "keywords": "payments, invoices",
        "meta": {"entities": ["Bob"], "topics": ["renewal"]},
        "abstract_json": {"slots": {"entities": ["Carol"], "topics": "roadmap"}},
    }

    assert service.segment_anchor_terms(record) == {
        "Alice",
        "Bob",
        "Carol",
        "payments",
        "invoices",
        "renewal",
        "roadmap",
    }


def test_time_refs_overlap_allows_coarse_only_overlap() -> None:
    """Coarse-only overlap is weak; specific mismatches split."""
    service = RecompositionSegmentationService()

    assert service.time_refs_overlap({"2026-04-28"}, {"2026-04-28"})
    assert not service.time_refs_overlap(
        {"2026-04-28", "10:00"},
        {"2026-04-28", "11:00"},
    )
    assert service.time_refs_overlap(
        {"2026-04-28", "10:00"},
        {"2026-04-28", "10:00"},
    )


def test_sequential_segments_hard_split_on_source_segment_boundary() -> None:
    """Benchmark source segment changes force a hard split."""
    service = RecompositionSegmentationService()
    entries = [
        _entry(0, 0, time_refs=["2026-04-28"], source_segment_index=0),
        _entry(1, 1, time_refs=["2026-04-28"], source_segment_index=0),
        _entry(2, 2, time_refs=["2026-04-28"], source_segment_index=1),
    ]

    segments = service.build_recomposition_segments(entries)

    assert [segment["msg_range"] for segment in segments] == [[0, 1], [2, 2]]


def test_anchorless_cluster_respects_token_cap() -> None:
    """Anchorless entries still obey the full-recompose token cap."""
    service = RecompositionSegmentationService()
    entries = [
        _entry(0, 0, anchors=[], tokens=_RECOMPOSE_CLUSTER_MAX_TOKENS - 100),
        _entry(1, 1, anchors=[], tokens=200),
    ]

    segments = service.build_anchor_clustered_segments(entries)

    assert [segment["msg_range"] for segment in segments] == [[0, 0], [1, 1]]


def test_oversized_seed_is_emitted_as_single_segment() -> None:
    """Oversized seed entries do not poison the next cluster."""
    service = RecompositionSegmentationService()
    entries = [
        _entry(0, 0, anchors=["Alice"], tokens=_RECOMPOSE_CLUSTER_MAX_TOKENS + 1),
        _entry(1, 1, anchors=["Alice"], tokens=10),
    ]

    segments = service.build_anchor_clustered_segments(entries)

    assert [segment["msg_range"] for segment in segments] == [[0, 0], [1, 1]]


def test_finalize_segment_dedupes_sources_and_uris() -> None:
    """Finalization preserves payload shape and dedupes source records."""
    service = RecompositionSegmentationService()
    source_uri = "opencortex://tenant/user/memories/events/e-000000-000000"
    entries = [
        _entry(
            0,
            0,
            text="hello",
            uri=source_uri,
            immediate_uris=["imm-a"],
            superseded_merged_uris=["merged-a"],
        ),
        _entry(
            1,
            1,
            text="",
            uri=source_uri,
            immediate_uris=["imm-a", "imm-b"],
            superseded_merged_uris=["merged-a"],
        ),
    ]

    segment = service.finalize_recomposition_segment(entries)

    assert segment["messages"] == ["hello"]
    assert segment["immediate_uris"] == ["imm-a", "imm-b"]
    assert segment["superseded_merged_uris"] == ["merged-a"]
    assert segment["msg_range"] == [0, 1]
    assert len(segment["source_records"]) == 1
