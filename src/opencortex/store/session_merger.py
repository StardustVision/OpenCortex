# SPDX-License-Identifier: Apache-2.0
"""Session immediate-to-merged primary record flow."""

from __future__ import annotations

from opencortex.core.context import Context, Vectorize
from opencortex.core.user_id import UserIdentifier
from opencortex.core.identity import IdentityProfile
from opencortex.retrieve.types import ContextType
from opencortex.store.common import build_abstract_json, memory_object_payload
from opencortex.store.embedder import StoreEmbedder
from opencortex.store.schemas import PrimaryRecordInput, StoredRecord
from opencortex.store.session_buffer import SessionBuffer, SessionKey
from opencortex.store.events import StoreEvents
from opencortex.store.types import MemoryCategory, SessionRecordLayer
from opencortex.storage.cortex_namespace import CortexNamespace
from opencortex.utils.text import smart_truncate
from opencortex.writer.primary_record_writer import PrimaryRecordWriter


class SessionMerger:
    """Merge buffered immediate records into merged primary records."""

    def __init__(
        self,
        *,
        buffer: SessionBuffer,
        namespace: CortexNamespace,
        embedder: StoreEmbedder,
        writer: PrimaryRecordWriter,
        events: StoreEvents,
        storage: object | None = None,
        collection_resolver: object | None = None,
    ) -> None:
        self.buffer = buffer
        self.namespace = namespace
        self.embedder = embedder
        self.writer = writer
        self.events = events
        self.storage = storage
        self.collection_resolver = collection_resolver

    async def merge_unmerged(
        self,
        key: SessionKey,
        *,
        profile: IdentityProfile,
    ) -> StoredRecord | None:
        """Synchronously merge the current unmerged session buffer."""
        snapshot = self.buffer.snapshot(key)
        if snapshot is None:
            return None

        content = "\n\n".join(snapshot.messages)
        msg_range = [
            snapshot.start_msg_index,
            snapshot.start_msg_index + len(snapshot.messages) - 1,
        ]
        record_input = self.build_merged_record(
            profile=profile,
            content=content,
            msg_range=msg_range,
            source_uris=snapshot.immediate_uris,
        )
        await self.embedder.embed_context(record_input.ctx)
        stored = await self.writer.write(record_input)
        await self.remove_immediate_records(snapshot.immediate_uris)
        self.events.session_merged(
            profile=profile,
            merged_uri=stored.uri,
            source_uris=snapshot.immediate_uris,
            content=content,
            record=dict(stored.record),
        )
        return stored

    async def remove_immediate_records(self, source_uris: list[str]) -> None:
        """Remove immediate RAG records after the merged leaf is written."""
        if self.storage is None or self.collection_resolver is None:
            return
        collection = self.collection_resolver()
        for uri in source_uris:
            if uri:
                await self.storage.remove_by_uri(collection, uri)

    def build_merged_record(
        self,
        *,
        profile: IdentityProfile,
        content: str,
        msg_range: list[int],
        source_uris: list[str],
    ) -> PrimaryRecordInput:
        """Build one merged-leaf primary record from buffered messages."""
        session_id = profile.session_id
        uri = self.namespace.session_merged_uri(
            session_id,
            msg_range,
            profile=profile,
        )
        parent_uri = self.namespace.session_events_parent(session_id, profile=profile)
        abstract = smart_truncate(content.strip(), 240)
        meta = {
            "project_id": profile.project_id,
            "layer": str(SessionRecordLayer.MERGED),
            "session_id": session_id,
            "msg_range": msg_range,
            "source_uri": parent_uri,
            "recomposition_stage": "online_tail",
            "source_uris": list(source_uris),
        }
        ctx = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=True,
            abstract=abstract,
            overview="",
            context_type=ContextType.MEMORY,
            category=str(MemoryCategory.EVENTS),
            meta=meta,
            session_id=session_id,
            user=UserIdentifier(profile.tenant_id, profile.user_id),
        )
        ctx.vectorize = Vectorize(content)
        abstract_json = build_abstract_json(
            uri=uri,
            context_type=ContextType.MEMORY,
            category=str(MemoryCategory.EVENTS),
            abstract=abstract,
            overview="",
            content=content,
            entities=[],
            meta=meta,
            keywords=[],
            parent_uri=parent_uri,
            session_id=session_id,
        )
        return PrimaryRecordInput(
            ctx=ctx,
            abstract_json=abstract_json,
            object_payload=memory_object_payload(abstract_json, is_leaf=True),
            effective_category=str(MemoryCategory.EVENTS),
            keywords="",
            entities=[],
            meta=meta,
            context_type=ContextType.MEMORY,
            session_id=session_id,
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            content=content,
        )
