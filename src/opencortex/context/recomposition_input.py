# SPDX-License-Identifier: Apache-2.0
"""Input and record assembly for session recomposition."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List

from opencortex.context.recomposition_segmentation import (
    RecompositionSegmentationService,
    _merge_unique_strings,
    _split_topic_values,
)
from opencortex.context.recomposition_types import RecompositionEntry
from opencortex.context.session_records import record_msg_range, record_text
from opencortex.services.memory_filters import FilterExpr

if TYPE_CHECKING:
    from opencortex.context.manager import ContextManager, ConversationBuffer

_RECOMPOSE_TAIL_MAX_MERGED_LEAVES = 6
_RECOMPOSE_TAIL_MAX_MESSAGES = 24


class RecompositionInputService:
    """Builds records and entries consumed by recomposition orchestration."""

    def __init__(self, manager: "ContextManager") -> None:
        """Create an input service bound to one context manager."""
        self._manager = manager
        self._segmentation = RecompositionSegmentationService()

    async def select_tail_merged_records(
        self,
        *,
        session_id: str,
        source_uri: str,
    ) -> List[Dict[str, Any]]:
        """Select a bounded recent merged-tail window for online recomposition."""
        merged_records = await self._manager._session_records.load_merged(
            session_id=session_id,
            source_uri=source_uri,
        )
        if not merged_records:
            return []

        selected: List[Dict[str, Any]] = []
        selected_message_count = 0
        for record in reversed(merged_records):
            msg_range = record_msg_range(record)
            if msg_range is None:
                continue
            width = (msg_range[1] - msg_range[0]) + 1
            if len(selected) >= _RECOMPOSE_TAIL_MAX_MERGED_LEAVES:
                break
            if (
                selected
                and (selected_message_count + width) > _RECOMPOSE_TAIL_MAX_MESSAGES
            ):
                break
            selected.append(record)
            selected_message_count += width
        selected.reverse()
        return selected

    async def load_immediate_records(
        self,
        immediate_uris: List[str],
    ) -> List[Dict[str, Any]]:
        """Load immediate records and return them ordered by message index."""
        if not immediate_uris:
            return []
        manager = self._manager
        records = await manager._orchestrator._storage.filter(
            manager._orchestrator._get_collection(),
            FilterExpr.eq("uri", *immediate_uris).to_dict(),
            limit=max(len(immediate_uris), 1),
        )
        by_uri = {
            str(record.get("uri", "")).strip(): record
            for record in records
            if str(record.get("uri", "")).strip()
        }
        ordered: List[Dict[str, Any]] = []
        for uri in immediate_uris:
            record = by_uri.get(str(uri).strip())
            if record is not None:
                ordered.append(record)
        return ordered

    async def aggregate_records_metadata(
        self,
        records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Collect anchor metadata from already loaded source records."""
        if not records:
            return {}

        entities: List[str] = []
        time_refs: List[str] = []
        topics: List[str] = []
        event_date = ""

        for record in records:
            meta = dict(record.get("meta") or {})
            abstract_json = record.get("abstract_json")
            slots = (
                abstract_json.get("slots", {})
                if isinstance(abstract_json, dict)
                else {}
            )

            entities = _merge_unique_strings(
                entities,
                record.get("entities"),
                meta.get("entities"),
                slots.get("entities"),
            )
            time_refs = _merge_unique_strings(
                time_refs,
                meta.get("time_refs"),
                slots.get("time_refs"),
                record.get("event_date"),
                meta.get("event_date"),
            )
            topics = _merge_unique_strings(
                topics,
                _split_topic_values(record.get("keywords")),
                _split_topic_values(meta.get("keywords")),
                _split_topic_values(meta.get("topics")),
                _split_topic_values(slots.get("topics")),
            )

            if not event_date:
                event_date = str(
                    record.get("event_date") or meta.get("event_date") or ""
                ).strip()

        merged_meta: Dict[str, Any] = {}
        if entities:
            merged_meta["entities"] = entities
        if time_refs:
            merged_meta["time_refs"] = time_refs
        if topics:
            merged_meta["topics"] = topics
        if event_date:
            merged_meta["event_date"] = event_date
        return merged_meta

    async def build_recomposition_entries(
        self,
        *,
        snapshot: "ConversationBuffer",
        immediate_records: List[Dict[str, Any]],
        tail_records: List[Dict[str, Any]],
    ) -> List[RecompositionEntry]:
        """Build ordered recomposition entries from merged-tail plus immediates."""
        entries: List[RecompositionEntry] = []
        l2_by_uri = await self._load_tail_l2_content(tail_records)

        for record in tail_records:
            msg_range = record_msg_range(record)
            if msg_range is None:
                continue
            uri = str(record.get("uri", "") or "").strip()
            text = l2_by_uri.get(uri, "") or record_text(record)
            if not text:
                continue
            entries.append(
                RecompositionEntry(
                    text=text,
                    uri=uri,
                    msg_start=msg_range[0],
                    msg_end=msg_range[1],
                    token_count=max(self._manager._estimate_tokens(text), 1),
                    anchor_terms=self._segmentation.segment_anchor_terms(record),
                    time_refs=self._segmentation.segment_time_refs(record),
                    source_record=record,
                    immediate_uris=[],
                    superseded_merged_uris=([uri] if uri else []),
                    source_segment_index=None,
                )
            )

        by_uri = {
            str(record.get("uri", "")).strip(): record
            for record in immediate_records
            if str(record.get("uri", "")).strip()
        }
        for offset, text in enumerate(snapshot.messages):
            uri = (
                snapshot.immediate_uris[offset]
                if offset < len(snapshot.immediate_uris)
                else ""
            )
            normalized_uri = str(uri or "").strip()
            record = by_uri.get(normalized_uri)
            if record is None:
                fallback_index = snapshot.start_msg_index + offset
                record = {
                    "uri": normalized_uri,
                    "abstract": text,
                    "meta": {"msg_index": fallback_index},
                    "keywords": "",
                    "entities": [],
                }
            msg_range = record_msg_range(record)
            if msg_range is None:
                msg_index = snapshot.start_msg_index + offset
                msg_range = (msg_index, msg_index)
            entries.append(
                RecompositionEntry(
                    text=str(text),
                    uri=normalized_uri,
                    msg_start=msg_range[0],
                    msg_end=msg_range[1],
                    token_count=max(self._manager._estimate_tokens(text), 1),
                    anchor_terms=self._segmentation.segment_anchor_terms(record),
                    time_refs=self._segmentation.segment_time_refs(record),
                    source_record=record,
                    immediate_uris=([normalized_uri] if normalized_uri else []),
                    superseded_merged_uris=[],
                    source_segment_index=None,
                )
            )

        entries.sort(
            key=lambda entry: (
                int(entry["msg_start"]),
                int(entry["msg_end"]),
                str(entry["uri"]),
            )
        )
        return entries

    async def _load_tail_l2_content(
        self,
        tail_records: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        """Read L2 content for merged-tail records when CortexFS is available."""
        fs = getattr(self._manager._orchestrator, "_fs", None)
        tail_uris = [
            str(record.get("uri", "") or "").strip()
            for record in tail_records
            if record.get("uri")
        ]
        if not fs or not tail_uris:
            return {}

        async def _read_l2(uri: str) -> str:
            try:
                return await fs.read_file(f"{uri}/content.md")
            except Exception:
                return ""

        l2_contents = await asyncio.gather(*[_read_l2(uri) for uri in tail_uris])
        return dict(zip(tail_uris, l2_contents, strict=True))
