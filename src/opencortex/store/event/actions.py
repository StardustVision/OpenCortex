# SPDX-License-Identifier: Apache-2.0
"""Event actions for store/session side effects."""

from __future__ import annotations

from typing import Protocol, TypeVar

import structlog

from opencortex.store.event.events import (
    CheckUpdateEvent,
    MemoryEvent,
    SessionMergedEvent,
    SessionTurnStoredEvent,
)
from opencortex.store.session.buffer import SessionBuffer
from opencortex.store.session.merger import SessionMerger
from opencortex.store.writer.cortex_storage_writer import CortexStorageWriter
from opencortex.store.writer.entity_index_writer import EntityIndexWriter
from opencortex.store.writer.reason_tree_build_writer import ReasonTreeBuildWriter
from opencortex.store.writer.reason_tree_index_writer import ReasonTreeIndexWriter
from opencortex.store.writer.search_index_writer import (
    AnchorIndex,
    FactIndex,
    SearchIndexWriter,
)
from opencortex.store.writer.semantic_derive_writer import SemanticDeriveWriter
from opencortex.store.writer.session_cleanup_writer import SessionCleanupWriter

logger = structlog.get_logger(__name__)

EventT = TypeVar("EventT", bound=MemoryEvent)


class EventAction(Protocol[EventT]):
    """One async side-effect triggered by one event type."""

    name: str
    event_type: type[EventT]

    async def run(self, event: EventT) -> None:
        """Run the side-effect for one event."""
        ...


class SearchIndexAction:
    """Coordinate search index writes for stored primary records."""

    name = "search_index"
    event_type = MemoryEvent

    def __init__(
        self,
        *,
        vector_store: object,
        collection_resolver: object,
        embedder: object = None,
    ) -> None:
        self.writer = SearchIndexWriter(
            vector_store=vector_store,
            collection_resolver=collection_resolver,
            embedder=embedder,
        )

    async def run(self, event: MemoryEvent) -> None:
        """Run search index writes for applicable events."""
        await self.writer.write(event)

    def anchor_indexes(self, event: MemoryEvent) -> list[AnchorIndex]:
        """Build anchor index entries through the search index writer."""
        return self.writer.anchor_indexes(event)

    def fact_indexes(self, event: MemoryEvent) -> list[FactIndex]:
        """Build fact index entries through the search index writer."""
        return self.writer.fact_indexes(event)


class EntityIndexAction:
    """Coordinate entity index writes for stored primary records."""

    name = "entity_index"
    event_type = MemoryEvent

    def __init__(
        self,
        *,
        vector_store: object,
        collection_resolver: object,
        embedder: object = None,
    ) -> None:
        self.writer = EntityIndexWriter(
            vector_store=vector_store,
            collection_resolver=collection_resolver,
            embedder=embedder,
        )

    async def run(self, event: MemoryEvent) -> None:
        """Run entity index writes for applicable events."""
        await self.writer.write(event)


class SemanticDeriveAction:
    """Complete raw primary records before secondary side effects run."""

    name = "semantic_derive"
    event_type = MemoryEvent

    def __init__(
        self,
        *,
        vector_store: object,
        collection_resolver: object,
        llm_completion: object,
        embedder: object = None,
    ) -> None:
        self.writer = SemanticDeriveWriter(
            vector_store=vector_store,
            collection_resolver=collection_resolver,
            llm_completion=llm_completion,
            embedder=embedder,
        )

    async def run(self, event: MemoryEvent) -> None:
        """Derive semantic fields and update the event record for later actions."""
        ready_record = await self.writer.write(event)
        if hasattr(event, "record"):
            event.record = ready_record


class CortexStorageAction:
    """Coordinate CortexStorage writes after primary storage succeeds."""

    name = "cortex_storage"
    event_type = MemoryEvent

    def __init__(self, *, cortex_storage: object) -> None:
        self.writer = CortexStorageWriter(cortex_storage=cortex_storage)

    async def run(self, event: MemoryEvent) -> None:
        """Run CortexStorage writes for applicable events."""
        await self.writer.write(event)


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
            self.buffer.freeze_ready_chunks(key)
            while self.buffer.has_pending_merge(key):
                await self.merger.merge_unmerged(
                    key,
                    profile=event.profile,
                )


class SessionCleanupAction:
    """Coordinate cleanup after immediate records are merged."""

    name = "session_cleanup"
    event_type = SessionMergedEvent

    def __init__(self, *, vector_store: object, collection_resolver: object) -> None:
        self.writer = SessionCleanupWriter(
            vector_store=vector_store,
            collection_resolver=collection_resolver,
        )

    async def run(self, event: SessionMergedEvent) -> None:
        """Run cleanup writes for merged-session events."""
        await self.writer.write(event)


class ReasonTreeIndexAction:
    """Coordinate reason-tree retrieval projection writes."""

    name = "reason_tree_index"
    event_type = MemoryEvent

    def __init__(
        self,
        *,
        vector_store: object,
        collection_resolver: object,
        namespace: object,
        embedder: object = None,
    ) -> None:
        self.writer = ReasonTreeIndexWriter(
            vector_store=vector_store,
            collection_resolver=collection_resolver,
            namespace=namespace,
            embedder=embedder,
        )

    async def run(self, event: MemoryEvent) -> None:
        """Write reason-tree projections for ready primary records."""
        await self.writer.write(event)


class ReasonTreeBuildAction:
    """Coordinate LLM-enhanced reason-tree index writes."""

    name = "reason_tree_build"
    event_type = MemoryEvent

    def __init__(
        self,
        *,
        vector_store: object,
        collection_resolver: object,
        llm_completion: object,
        embedder: object = None,
    ) -> None:
        self.writer = ReasonTreeBuildWriter(
            vector_store=vector_store,
            collection_resolver=collection_resolver,
            llm_completion=llm_completion,
            embedder=embedder,
        )

    async def run(self, event: MemoryEvent) -> None:
        """Build enhanced reason-tree index nodes for applicable records."""
        await self.writer.write(event)


class CheckUpdateAction:
    """Reserve update checking outside the primary write path."""

    name = "check_update"
    event_type = CheckUpdateEvent

    async def run(self, event: CheckUpdateEvent) -> None:
        """Accept a primary write for future mutation detection."""
        logger.debug(
            "check_update_action_accepted",
            uri=event.uri,
            context_type=event.context_type,
            category=event.category,
        )
