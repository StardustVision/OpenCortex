# SPDX-License-Identifier: Apache-2.0
"""Writers for secondary search index records."""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import BaseModel

from opencortex.store.event.events import MemoryEvent
from opencortex.store.writer.event_payload import (
    digest,
    event_record_id,
    event_uri,
    primary_record,
    record_abstract_json,
)
from opencortex.utils.facts import sorted_answerable_facts
from opencortex.vector.payloads import (
    AnchorIndexPayload,
    FactIndexPayload,
    VectorPayloadSurface,
)

logger = structlog.get_logger(__name__)


class AnchorIndex(BaseModel):
    """Search index entry for entity/topic/keyword anchors."""

    text: str
    source_uri: str
    source_record_id: str
    anchor_type: str = "term"
    score: float = 1.0


class FactIndex(BaseModel):
    """Search index entry for concrete fact sentences."""

    text: str
    source_uri: str
    source_record_id: str
    score: float = 1.0


class SearchIndexWriter:
    """Write AnchorIndex and FactIndex records for primary records."""

    max_fact_points = 12
    min_fact_length = 8
    max_fact_length = 240

    def __init__(
        self,
        *,
        vector_store: Any,
        collection_resolver: Any,
        embedder: Any = None,
    ) -> None:
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.embedder = embedder

    async def write(self, event: MemoryEvent) -> None:
        """Write search indexes for one primary-record event."""
        record = primary_record(event)
        if (
            not record
            or not bool(record.get("retrieval_ready", False))
            or not bool(record.get("is_leaf", False))
        ):
            return
        records = self.search_records(event)
        if not records:
            return
        self.embed_records(records)
        for index_record in records:
            await self.vector_store.upsert(self.collection_resolver(), index_record)

    def anchor_indexes(self, event: MemoryEvent) -> list[AnchorIndex]:
        """Build anchor index entries from the event record payload."""
        record = primary_record(event)
        raw_anchors: list[tuple[str, str]] = []
        for entity in record.get("entities") or []:
            raw_anchors.append(("entity", str(entity)))

        keywords = str(record.get("keywords", "") or "")
        if keywords:
            raw_anchors.extend(("keyword", part) for part in keywords.split(","))

        abstract_json = record_abstract_json(record)
        for anchor in abstract_json.get("anchors") or []:
            if not isinstance(anchor, dict):
                continue
            text = str(anchor.get("text") or anchor.get("value") or "")
            anchor_type = str(anchor.get("anchor_type") or "anchor")
            raw_anchors.append((anchor_type, text))

        meta = dict(record.get("meta") or {})
        for handle in meta.get("anchor_handles") or []:
            raw_anchors.append(("handle", str(handle)))

        seen: set[str] = set()
        indexes: list[AnchorIndex] = []
        for anchor_type, text in raw_anchors:
            normalized = str(text).strip()
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            indexes.append(
                AnchorIndex(
                    text=normalized,
                    source_uri=event_uri(event),
                    source_record_id=event_record_id(event),
                    anchor_type=anchor_type,
                )
            )
        return indexes

    def fact_indexes(self, event: MemoryEvent) -> list[FactIndex]:
        """Build fact index entries from abstract_json.fact_points."""
        fact_points = record_abstract_json(primary_record(event)).get(
            "fact_points",
            [],
        )
        if not isinstance(fact_points, list):
            fact_points = []
        seen: set[str] = set()
        indexes: list[FactIndex] = []
        for fact in sorted_answerable_facts(fact_points):
            text = fact
            if (
                len(text) < self.min_fact_length
                or len(text) > self.max_fact_length
                or text.casefold() in seen
            ):
                continue
            seen.add(text.casefold())
            indexes.append(
                FactIndex(
                    text=text,
                    source_uri=event_uri(event),
                    source_record_id=event_record_id(event),
                )
            )
            if len(indexes) >= self.max_fact_points:
                break
        return indexes

    def search_records(self, event: MemoryEvent) -> list[dict[str, Any]]:
        """Build persisted search index records for one event."""
        record = primary_record(event)
        anchor_records = [
            self.anchor_record(event, record, index)
            for index in self.anchor_indexes(event)
        ]
        fact_records = [
            self.fact_record(event, record, index) for index in self.fact_indexes(event)
        ]
        return anchor_records + fact_records

    def anchor_record(
        self,
        event: MemoryEvent,
        record: dict[str, Any],
        index: AnchorIndex,
    ) -> dict[str, Any]:
        """Build one AnchorIndex storage record."""
        return self.index_record(
            event=event,
            record=record,
            index_name="AnchorIndex",
            retrieval_surface=VectorPayloadSurface.ANCHOR_INDEX,
            text=index.text,
            uri=(
                f"{event_uri(event)}/anchor_indexes/"
                f"{digest(f'{index.anchor_type}:{index.text}')}"
            ),
            anchor_type=index.anchor_type,
            index_score=index.score,
        )

    def fact_record(
        self,
        event: MemoryEvent,
        record: dict[str, Any],
        index: FactIndex,
    ) -> dict[str, Any]:
        """Build one FactIndex storage record."""
        return self.index_record(
            event=event,
            record=record,
            index_name="FactIndex",
            retrieval_surface=VectorPayloadSurface.FACT_INDEX,
            text=index.text,
            uri=f"{event_uri(event)}/fact_indexes/{digest(index.text)}",
            index_score=index.score,
        )

    def index_record(
        self,
        *,
        event: MemoryEvent,
        record: dict[str, Any],
        index_name: str,
        retrieval_surface: str,
        text: str,
        uri: str,
        anchor_type: str = "",
        index_score: float = 1.0,
    ) -> dict[str, Any]:
        """Build one generic search index storage record."""
        meta = dict(record.get("meta") or {})
        meta.update(
            {
                "index_name": index_name,
                "source_uri": event_uri(event),
                "source_record_id": event_record_id(event),
            }
        )
        base = {
            "id": uri,
            "uri": uri,
            "parent_uri": event_uri(event),
            "context_type": record.get("context_type", ""),
            "category": record.get("category", ""),
            "abstract": text,
            "overview": text,
            "content": text,
            "retrieval_surface": retrieval_surface,
            "source_uri": event_uri(event),
            "source_record_id": event_record_id(event),
            "source_tenant_id": event.tenant_id,
            "source_user_id": event.user_id,
            "tenant_id": event.tenant_id,
            "user_id": event.user_id,
            "project_id": event.project_id,
            "scope": record.get("scope", ""),
            "session_id": getattr(event, "session_id", ""),
            "entities": record.get("entities", []),
            "keywords": record.get("keywords", ""),
            "anchor_hits": record.get("anchor_hits", []),
            "memory_kind": record.get("memory_kind", ""),
            "index_score": index_score,
            "meta": meta,
        }
        if retrieval_surface == VectorPayloadSurface.ANCHOR_INDEX:
            return AnchorIndexPayload(
                **base,
                anchor_type=anchor_type or "term",
            ).to_record()
        return FactIndexPayload(**base).to_record()

    def embed_records(self, records: list[dict[str, Any]]) -> None:
        """Attach required vectors to search index records."""
        if self.embedder is None:
            raise RuntimeError("SearchIndexWriter requires an embedder")
        texts = [str(record.get("overview", "") or "") for record in records]
        results = self.embedder.embed_batch(texts)
        for record, result in zip(records, results, strict=False):
            if getattr(result, "dense_vector", None):
                record["vector"] = result.dense_vector
            else:
                raise ValueError("Search index embedding returned no dense vector")
            if getattr(result, "sparse_vector", None):
                record["sparse_vector"] = result.sparse_vector
