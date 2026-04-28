# SPDX-License-Identifier: Apache-2.0
"""Pure segmentation algorithms for session recomposition."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Set

from opencortex.context.recomposition_types import RecompositionEntry

_SEGMENT_MAX_MESSAGES = 16
_SEGMENT_MAX_TOKENS = 1200
_SEGMENT_MIN_MESSAGES = 2
_RECOMPOSE_CLUSTER_MAX_TOKENS = 6_000
_RECOMPOSE_CLUSTER_MAX_MESSAGES = 60
_RECOMPOSE_CLUSTER_JACCARD_THRESHOLD = 0.15
_COARSE_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COARSE_HUMAN_DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]+,\s+\d{4}$")
_COARSE_WEEKDAY_RE = re.compile(
    r"^(?:周[一二三四五六日天]|星期[一二三四五六日天]|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)$",
    re.IGNORECASE,
)


def _merge_unique_strings(*groups: Any) -> List[str]:
    """Return a stable ordered union of non-empty string values."""
    merged: List[str] = []
    for group in groups:
        if not group:
            continue
        values = [group] if isinstance(group, str) else list(group)
        for value in values:
            normalized = str(value).strip()
            if normalized and normalized not in merged:
                merged.append(normalized)
    return merged


def _split_topic_values(raw_value: Any) -> List[str]:
    """Normalize topic-like values, splitting comma-separated strings."""
    if not raw_value:
        return []
    if isinstance(raw_value, str):
        return [
            token.strip() for token in raw_value.split(",") if token and token.strip()
        ]
    return _merge_unique_strings(raw_value)


@dataclass(frozen=True)
class RecompositionSegmentationService:
    """Deterministic segmentation logic for recomposition entries."""

    segment_max_messages: int = _SEGMENT_MAX_MESSAGES
    segment_max_tokens: int = _SEGMENT_MAX_TOKENS
    segment_min_messages: int = _SEGMENT_MIN_MESSAGES
    cluster_max_tokens: int = _RECOMPOSE_CLUSTER_MAX_TOKENS
    cluster_max_messages: int = _RECOMPOSE_CLUSTER_MAX_MESSAGES
    cluster_jaccard_threshold: float = _RECOMPOSE_CLUSTER_JACCARD_THRESHOLD

    def segment_anchor_terms(self, record: Dict[str, Any]) -> Set[str]:
        """Extract coarse anchor terms used for sequential merge boundaries."""
        meta = dict(record.get("meta") or {})
        abstract_json = record.get("abstract_json")
        slots = (
            abstract_json.get("slots", {}) if isinstance(abstract_json, dict) else {}
        )
        return set(
            _merge_unique_strings(
                record.get("entities"),
                meta.get("entities"),
                slots.get("entities"),
                _split_topic_values(record.get("keywords")),
                _split_topic_values(meta.get("topics")),
                _split_topic_values(slots.get("topics")),
            )
        )

    def segment_time_refs(self, record: Dict[str, Any]) -> Set[str]:
        """Extract normalized time references used for segment boundaries."""
        meta = dict(record.get("meta") or {})
        abstract_json = record.get("abstract_json")
        slots = (
            abstract_json.get("slots", {}) if isinstance(abstract_json, dict) else {}
        )
        return set(
            _merge_unique_strings(
                meta.get("time_refs"),
                slots.get("time_refs"),
                record.get("event_date"),
                meta.get("event_date"),
            )
        )

    def is_coarse_time_ref(self, value: str) -> bool:
        """Return whether one time ref is too coarse to force matching."""
        normalized = str(value or "").strip()
        if not normalized:
            return False
        return bool(
            _COARSE_ISO_DATE_RE.fullmatch(normalized)
            or _COARSE_HUMAN_DATE_RE.fullmatch(normalized)
            or _COARSE_WEEKDAY_RE.fullmatch(normalized)
        )

    def time_refs_overlap(self, left: Set[str], right: Set[str]) -> bool:
        """Return whether two time-ref sets meaningfully overlap."""
        shared = set(left).intersection(right)
        if not shared:
            return False

        left_specific = {value for value in left if not self.is_coarse_time_ref(value)}
        right_specific = {
            value for value in right if not self.is_coarse_time_ref(value)
        }
        if not left_specific or not right_specific:
            return True

        return bool(left_specific.intersection(right_specific))

    def build_recomposition_segments(
        self,
        entries: List[RecompositionEntry],
    ) -> List[Dict[str, Any]]:
        """Split ordered recomposition entries into semantic segments."""
        if not entries:
            return []

        segments: List[Dict[str, Any]] = []
        current: List[RecompositionEntry] = []
        current_tokens = 0
        current_messages = 0

        for entry in entries:
            entry_messages = (int(entry["msg_end"]) - int(entry["msg_start"])) + 1
            should_split = False
            if current:
                prev_segment_index = current[-1]["source_segment_index"]
                entry_segment_index = entry["source_segment_index"]
                if (
                    prev_segment_index is not None
                    and entry_segment_index is not None
                    and prev_segment_index != entry_segment_index
                ):
                    should_split = True
                else:
                    current_time_refs: Set[str] = set()
                    for item in current:
                        current_time_refs.update(item["time_refs"])
                    if (
                        current_messages >= self.segment_max_messages
                        or current_tokens + int(entry["token_count"])
                        > self.segment_max_tokens
                        or (
                            current_messages >= self.segment_min_messages
                            and current_time_refs
                            and entry["time_refs"]
                            and not self.time_refs_overlap(
                                current_time_refs,
                                entry["time_refs"],
                            )
                        )
                    ):
                        should_split = True

            if should_split:
                segments.append(self.finalize_recomposition_segment(current))
                current = []
                current_tokens = 0
                current_messages = 0

            current.append(entry)
            current_tokens += int(entry["token_count"])
            current_messages += max(entry_messages, 1)

        if current:
            segments.append(self.finalize_recomposition_segment(current))

        return segments

    def build_anchor_clustered_segments(
        self,
        entries: List[RecompositionEntry],
    ) -> List[Dict[str, Any]]:
        """Cluster entries by anchor Jaccard similarity for full recompose."""
        if not entries:
            return []

        segments: List[Dict[str, Any]] = []
        current: List[RecompositionEntry] = []
        current_anchors: Set[str] = set()
        current_tokens = 0
        current_messages = 0

        def _within_caps_with(entry_tokens: int, entry_messages: int) -> bool:
            return (
                current_tokens + entry_tokens <= self.cluster_max_tokens
                and current_messages + max(entry_messages, 1)
                <= self.cluster_max_messages
            )

        def _seed_with(entry: RecompositionEntry) -> None:
            nonlocal current, current_anchors, current_tokens, current_messages
            entry_msgs = int(entry["msg_end"]) - int(entry["msg_start"]) + 1
            current = [entry]
            current_anchors = set(entry["anchor_terms"] | entry["time_refs"])
            current_tokens = int(entry["token_count"])
            current_messages = max(entry_msgs, 1)

        for entry in entries:
            entry_anchors: Set[str] = entry["anchor_terms"] | entry["time_refs"]
            entry_messages = int(entry["msg_end"]) - int(entry["msg_start"]) + 1
            entry_tokens = int(entry["token_count"])

            if not current:
                _seed_with(entry)
                if (
                    current_tokens > self.cluster_max_tokens
                    or current_messages > self.cluster_max_messages
                ):
                    segments.append(self.finalize_recomposition_segment(current))
                    current = []
                    current_anchors = set()
                    current_tokens = 0
                    current_messages = 0
                continue

            within_caps = _within_caps_with(entry_tokens, entry_messages)

            if not entry_anchors:
                if within_caps:
                    current.append(entry)
                    current_tokens += entry_tokens
                    current_messages += max(entry_messages, 1)
                else:
                    segments.append(self.finalize_recomposition_segment(current))
                    current = [entry]
                    current_anchors = set()
                    current_tokens = entry_tokens
                    current_messages = max(entry_messages, 1)
                continue

            union = current_anchors | entry_anchors
            jaccard = (
                len(current_anchors & entry_anchors) / len(union) if union else 0.0
            )

            if jaccard >= self.cluster_jaccard_threshold and within_caps:
                current.append(entry)
                current_anchors = current_anchors | entry_anchors
                current_tokens += entry_tokens
                current_messages += max(entry_messages, 1)
            else:
                segments.append(self.finalize_recomposition_segment(current))
                current = [entry]
                current_anchors = set(entry_anchors)
                current_tokens = entry_tokens
                current_messages = max(entry_messages, 1)

        if current:
            segments.append(self.finalize_recomposition_segment(current))

        return segments

    def finalize_recomposition_segment(
        self,
        entries: List[RecompositionEntry],
    ) -> Dict[str, Any]:
        """Materialize one recomposition segment payload."""
        msg_starts = [int(entry["msg_start"]) for entry in entries]
        msg_ends = [int(entry["msg_end"]) for entry in entries]
        source_records: List[Dict[str, Any]] = []
        source_uris: Set[str] = set()
        for entry in entries:
            record = entry.get("source_record") or {}
            uri = str(record.get("uri", "") or "")
            if uri and uri in source_uris:
                continue
            if uri:
                source_uris.add(uri)
            source_records.append(record)
        return {
            "messages": [
                str(entry["text"]) for entry in entries if str(entry["text"]).strip()
            ],
            "immediate_uris": _merge_unique_strings(
                *[entry.get("immediate_uris", []) for entry in entries]
            ),
            "superseded_merged_uris": _merge_unique_strings(
                *[entry.get("superseded_merged_uris", []) for entry in entries]
            ),
            "msg_range": [min(msg_starts), max(msg_ends)],
            "source_records": source_records,
        }
