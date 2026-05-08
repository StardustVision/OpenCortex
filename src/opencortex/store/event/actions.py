# SPDX-License-Identifier: Apache-2.0
"""Event actions for store/session side effects."""

from __future__ import annotations

import logging
from hashlib import sha1
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from opencortex.store.event.events import (
    MemoryEvent,
    MemoryStoredEvent,
    SessionEndedEvent,
    SessionMergedEvent,
    SessionTurnStoredEvent,
)
from opencortex.store.session.buffer import SessionBuffer
from opencortex.store.session.merger import SessionMerger

logger = logging.getLogger(__name__)

EventT = TypeVar("EventT", bound=MemoryEvent)


class EventAction(Protocol[EventT]):
    """One async side-effect triggered by one event type."""

    name: str
    event_type: type[EventT]

    async def run(self, event: EventT) -> None:
        """Run the side-effect for one event."""
        ...


class AnchorIndex(BaseModel):
    """Search index entry for entity/topic/keyword anchors."""

    text: str
    source_uri: str
    source_record_id: str
    score: float = 1.0


class FactIndex(BaseModel):
    """Search index entry for concrete fact sentences."""

    text: str
    source_uri: str
    source_record_id: str
    score: float = 1.0


class SearchIndexAction:
    """Update AnchorIndex and FactIndex records for stored primary records."""

    name = "search_index"
    event_type = MemoryEvent

    def __init__(
        self,
        *,
        storage: Any,
        collection_resolver: Any,
        embedder: Any = None,
    ) -> None:
        self.storage = storage
        self.collection_resolver = collection_resolver
        self.embedder = embedder

    async def run(self, event: MemoryEvent) -> None:
        """Update search indexes for event types that carry a primary record."""
        record = primary_record(event)
        if not record or not bool(record.get("is_leaf", False)):
            return
        records = self.search_records(event)
        if not records:
            return
        self.embed_records(records)
        for index_record in records:
            await self.storage.upsert(self.collection_resolver(), index_record)

    def anchor_indexes(self, event: MemoryEvent) -> list[AnchorIndex]:
        """Build anchor index entries from the event record payload."""
        record = primary_record(event)
        anchors = []
        for field in ("entities", "topics"):
            values = record.get(field) or []
            if isinstance(values, list):
                anchors.extend(values)
        keywords = str(record.get("keywords", "") or "")
        if keywords:
            anchors.extend(part.strip() for part in keywords.split(","))
        if not isinstance(anchors, list):
            anchors = []
        return [
            AnchorIndex(
                text=str(anchor),
                source_uri=event_uri(event),
                source_record_id=event_record_id(event),
            )
            for anchor in anchors
            if str(anchor).strip()
        ]

    def fact_indexes(self, event: MemoryEvent) -> list[FactIndex]:
        """Build fact index entries from abstract_json.fact_points."""
        fact_points = record_abstract_json(primary_record(event)).get(
            "fact_points",
            [],
        )
        if not isinstance(fact_points, list):
            fact_points = []
        return [
            FactIndex(
                text=str(fact),
                source_uri=event_uri(event),
                source_record_id=event_record_id(event),
            )
            for fact in fact_points
            if str(fact).strip()
        ]

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
            retrieval_surface="anchor_index",
            text=index.text,
            uri=f"{event_uri(event)}/anchor_indexes/{digest(index.text)}",
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
            retrieval_surface="fact_index",
            text=index.text,
            uri=f"{event_uri(event)}/fact_indexes/{digest(index.text)}",
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
        return {
            "id": uri,
            "uri": uri,
            "parent_uri": event_uri(event),
            "context_type": record.get("context_type", ""),
            "category": record.get("category", ""),
            "abstract": text,
            "overview": text,
            "content": text,
            "is_leaf": True,
            "retrieval_surface": retrieval_surface,
            "source_uri": event_uri(event),
            "source_record_id": event_record_id(event),
            "source_tenant_id": event.tenant_id,
            "source_user_id": event.user_id,
            "project_id": event.project_id,
            "scope": record.get("scope", ""),
            "session_id": getattr(event, "session_id", ""),
            "entities": record.get("entities", []),
            "keywords": record.get("keywords", ""),
            "meta": meta,
        }

    def embed_records(self, records: list[dict[str, Any]]) -> None:
        """Embed search index records when an embedder is available."""
        if self.embedder is None:
            return
        try:
            texts = [str(record.get("overview", "") or "") for record in records]
            results = self.embedder.embed_batch(texts)
        except Exception as exc:
            logger.warning("[SearchIndexAction] embed_batch failed: %s", exc)
            return
        for record, result in zip(records, results, strict=False):
            if getattr(result, "dense_vector", None):
                record["vector"] = result.dense_vector
            if getattr(result, "sparse_vector", None):
                record["sparse_vector"] = result.sparse_vector


class EntityIndexAction:
    """Update the entity index for stored primary records."""

    name = "entity_index"
    event_type = MemoryEvent

    def __init__(self, *, entity_index: Any, collection_resolver: Any) -> None:
        self.entity_index = entity_index
        self.collection_resolver = collection_resolver

    async def run(self, event: MemoryEvent) -> None:
        """Update entity lookup state from primary record entities."""
        if self.entity_index is None:
            return
        record = primary_record(event)
        if not record:
            return
        entities = record.get("entities") or []
        if not entities:
            return
        self.entity_index.add(
            self.collection_resolver(),
            str(record.get("id") or event_record_id(event)),
            entities,
        )


class CortexFSAction:
    """Write primary record content to CortexFS after storage succeeds."""

    name = "cortex_fs"
    event_type = MemoryEvent

    def __init__(self, *, fs: Any) -> None:
        self.fs = fs

    async def run(self, event: MemoryEvent) -> None:
        """Write a CortexFS blob for event types with a primary record."""
        if self.fs is None:
            return
        record = primary_record(event)
        if not record:
            return
        await self.fs.write_context(
            uri=event_uri(event),
            content=event_content(event),
            abstract=str(record.get("abstract", "") or ""),
            abstract_json=record_abstract_json(record),
            overview=str(record.get("overview", "") or ""),
            is_leaf=bool(record.get("is_leaf", False)),
        )


class SessionMergeAction:
    """Merge buffered immediate messages after session turns."""

    name = "session_merge"
    event_type = SessionTurnStoredEvent

    def __init__(
        self,
        *,
        buffer: SessionBuffer,
        merger: SessionMerger,
    ) -> None:
        self.buffer = buffer
        self.merger = merger

    async def run(self, event: SessionTurnStoredEvent) -> None:
        """Merge the session buffer when the configured threshold is reached."""
        key = self.buffer.profile_key(event.profile)
        async with self.buffer.lock(key):
            if not self.buffer.should_merge(key):
                return
            await self.merger.merge_unmerged(
                key,
                profile=event.profile,
            )


class SessionCleanupAction:
    """Remove immediate records after they are merged into a leaf."""

    name = "session_cleanup"
    event_type = SessionMergedEvent

    def __init__(self, *, storage: Any, collection_resolver: Any) -> None:
        self.storage = storage
        self.collection_resolver = collection_resolver

    async def run(self, event: SessionMergedEvent) -> None:
        """Delete source immediate records after merged leaf content is stored."""
        collection = self.collection_resolver()
        for uri in event.source_uris:
            if uri:
                await self.storage.remove_by_uri(collection, uri)


class ReasoningTreeIndexAction:
    """Reserved optional ReasoningTreeIndex hook."""

    name = "reasoning_tree_index"
    event_type = MemoryEvent

    async def run(self, event: MemoryEvent) -> None:
        """Reserve the action boundary without doing work yet."""
        logger.debug("[ReasoningTreeIndexAction] reserved event=%s", event.name)


class NoopAction:
    """Placeholder action for wiring tests and future side effects."""

    name = "noop"
    event_type = MemoryEvent

    async def run(self, event: MemoryEvent) -> None:
        """Accept the event and do nothing."""
        logger.debug("[NoopAction] accepted event=%s", event.name)


def primary_record(event: MemoryEvent) -> dict[str, Any]:
    """Return the primary record carried by a write event."""
    if isinstance(event, (MemoryStoredEvent, SessionMergedEvent, SessionEndedEvent)):
        return dict(event.record or {})
    return {}


def event_uri(event: MemoryEvent) -> str:
    """Return the primary record URI carried by an event."""
    if isinstance(event, MemoryStoredEvent):
        return event.uri
    if isinstance(event, SessionMergedEvent):
        return event.merged_uri
    if isinstance(event, SessionEndedEvent):
        return event.final_uri
    return ""


def event_record_id(event: MemoryEvent) -> str:
    """Return the primary record ID carried by an event."""
    record = primary_record(event)
    if record.get("id"):
        return str(record["id"])
    if isinstance(event, MemoryStoredEvent):
        return event.record_id
    return ""


def event_content(event: MemoryEvent) -> str:
    """Return the primary text carried by an event."""
    if isinstance(event, (MemoryStoredEvent, SessionMergedEvent, SessionEndedEvent)):
        return str(event.content or "")
    return ""


def record_abstract_json(record: dict[str, Any]) -> dict[str, Any]:
    """Return the abstract-json payload for a primary record."""
    return dict(record.get("abstract_json") or {})


def digest(text: str) -> str:
    """Return a stable short digest for index URIs."""
    return sha1(text.strip().lower().encode("utf-8")).hexdigest()[:16]
