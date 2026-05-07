# SPDX-License-Identifier: Apache-2.0
"""Session immediate-to-merged primary record flow."""

from __future__ import annotations

from opencortex.core.context import Context, Vectorize
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import get_effective_project_id
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
    ) -> None:
        self._buffer = buffer
        self._namespace = namespace
        self._embedder = embedder
        self._writer = writer
        self._events = events

    async def merge_unmerged(
        self,
        key: SessionKey,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> StoredRecord | None:
        """Synchronously merge the current unmerged session buffer."""
        snapshot = self._buffer.snapshot(key)
        if snapshot is None:
            return None

        content = "\n\n".join(snapshot.messages)
        msg_range = [
            snapshot.start_msg_index,
            snapshot.start_msg_index + len(snapshot.messages) - 1,
        ]
        record_input = self.build_merged_record(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            content=content,
            msg_range=msg_range,
            source_uris=snapshot.immediate_uris,
        )
        await self._embedder.embed_context(record_input.ctx)
        stored = await self._writer.write(record_input)
        self._events.session_merged(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            project_id=get_effective_project_id(),
            merged_uri=stored.uri,
            source_uris=snapshot.immediate_uris,
        )
        return stored

    def build_merged_record(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        content: str,
        msg_range: list[int],
        source_uris: list[str],
    ) -> PrimaryRecordInput:
        """Build one merged-leaf primary record from buffered messages."""
        uri = self._namespace.session_merged_uri(session_id, msg_range)
        parent_uri = self._namespace.session_events_parent(session_id)
        abstract = smart_truncate(content.strip(), 240)
        meta = {
            "layer": str(SessionRecordLayer.MERGED),
            "session_id": session_id,
            "msg_range": msg_range,
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
            user=UserIdentifier(tenant_id, user_id),
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
            tenant_id=tenant_id,
            user_id=user_id,
            content=content,
        )
