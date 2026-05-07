# SPDX-License-Identifier: Apache-2.0
"""Session end primary record flow."""

from __future__ import annotations

from typing import Any

from opencortex.core.context import Context, Vectorize
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import get_effective_identity
from opencortex.http.request_context import get_effective_project_id
from opencortex.retrieve.types import ContextType
from opencortex.services.memory_filters import FilterExpr
from opencortex.store.common import build_abstract_json, memory_object_payload
from opencortex.store.embedder import StoreEmbedder
from opencortex.store.schemas import (
    PrimaryRecordInput,
    SessionEndInput,
    SessionEndResult,
)
from opencortex.store.session_buffer import SessionBuffer
from opencortex.store.session_merger import SessionMerger
from opencortex.store.events import StoreEvents
from opencortex.store.types import MemoryCategory, SessionRecordLayer
from opencortex.storage.cortex_namespace import CortexNamespace
from opencortex.utils.text import smart_truncate
from opencortex.writer.primary_record_writer import PrimaryRecordWriter


class SessionEnder:
    """Close a session by writing the final session primary record."""

    def __init__(
        self,
        *,
        buffer: SessionBuffer,
        merger: SessionMerger,
        namespace: CortexNamespace,
        embedder: StoreEmbedder,
        writer: PrimaryRecordWriter,
        events: StoreEvents,
        storage: Any,
        collection_resolver: Any,
    ) -> None:
        self._buffer = buffer
        self._merger = merger
        self._namespace = namespace
        self._embedder = embedder
        self._writer = writer
        self._events = events
        self._storage = storage
        self._collection_resolver = collection_resolver

    async def end(self, input_: SessionEndInput) -> SessionEndResult:
        """Synchronously close session primary records, then publish events."""
        tenant_id, user_id = get_effective_identity()
        key = self._buffer.session_key(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=input_.session_id,
        )
        async with self._buffer.lock(key):
            self._buffer.touch(key)
            merged = await self._merger.merge_unmerged(
                key,
                session_id=input_.session_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            merged_records = await self.load_merged_records(input_.session_id)
            if merged is not None and not any(
                record.get("uri") == merged.uri for record in merged_records
            ):
                merged_records.append(dict(merged.record))

            if not merged_records:
                self._buffer.drop(key)
                return SessionEndResult(
                    session_id=input_.session_id,
                    write_status="empty",
                )

            record_input = self.build_session_end_record(
                session_id=input_.session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                merged_records=merged_records,
            )
            await self._embedder.embed_context(record_input.ctx)
            stored = await self._writer.write(record_input)
            self._buffer.drop(key)
            merged_uris = [str(record.get("uri", "")) for record in merged_records]
            self._events.session_ended(
                session_id=input_.session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=get_effective_project_id(),
                final_uri=stored.uri,
                merged_uris=merged_uris,
            )
            return SessionEndResult(
                session_id=input_.session_id,
                merged_uris=merged_uris,
                final_uri=stored.uri,
            )

    async def load_merged_records(self, session_id: str) -> list[dict[str, Any]]:
        """Load merged primary records for one session."""
        return await self._storage.filter(
            self._collection_resolver(),
            FilterExpr.all(
                FilterExpr.eq("session_id", session_id),
                FilterExpr.eq("meta.layer", str(SessionRecordLayer.MERGED)),
            ).to_dict(),
            limit=10000,
        )

    def build_session_end_record(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        merged_records: list[dict[str, Any]],
    ) -> PrimaryRecordInput:
        """Build the final session primary record."""
        uri = self._namespace.session_final_uri(session_id)
        parent_uri = self._namespace.session_events_parent(session_id)
        content = "\n\n".join(
            str(record.get("abstract", "") or "") for record in merged_records
        ).strip()
        abstract = smart_truncate(content, 240)
        merged_uris = [str(record.get("uri", "")) for record in merged_records]
        meta = {
            "layer": str(SessionRecordLayer.FINAL),
            "session_id": session_id,
            "merged_uris": merged_uris,
        }
        ctx = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=False,
            abstract=abstract,
            overview="",
            context_type=ContextType.MEMORY,
            category=str(MemoryCategory.EVENTS),
            meta=meta,
            session_id=session_id,
            user=UserIdentifier(tenant_id, user_id),
        )
        ctx.vectorize = Vectorize(content or abstract)
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
            object_payload=memory_object_payload(abstract_json, is_leaf=False),
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
