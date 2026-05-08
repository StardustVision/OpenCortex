# SPDX-License-Identifier: Apache-2.0
"""Session end primary record flow."""

from __future__ import annotations

from typing import Any

from opencortex.core.context import Context, Vectorize
from opencortex.core.identity import IdentityProfile
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import get_identity_profile
from opencortex.retrieve.types import ContextType
from opencortex.services.memory_filters import FilterExpr
from opencortex.storage.cortex_namespace import CortexNamespace
from opencortex.store.common import build_abstract_json, memory_object_payload
from opencortex.store.embedder import StoreEmbedder
from opencortex.store.event.events import StoreEvents
from opencortex.store.schemas import (
    PrimaryRecordInput,
    SessionEndInput,
    SessionEndResult,
)
from opencortex.store.session.buffer import SessionBuffer
from opencortex.store.session.merger import SessionMerger
from opencortex.store.types import MemoryCategory, SessionRecordLayer
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
        self.buffer = buffer
        self.merger = merger
        self.namespace = namespace
        self.embedder = embedder
        self.writer = writer
        self.events = events
        self.storage = storage
        self.collection_resolver = collection_resolver

    async def end(self, input_: SessionEndInput) -> SessionEndResult:
        """Synchronously close session primary records, then publish events."""
        profile = get_identity_profile(session_id=input_.session_id)
        key = self.buffer.profile_key(profile)
        async with self.buffer.lock(key):
            self.buffer.touch(key, profile)
            merged = await self.merger.merge_unmerged(
                key,
                profile=profile,
            )
            merged_records = await self.load_merged_records(input_.session_id)
            if merged is not None and not any(
                record.get("uri") == merged.uri for record in merged_records
            ):
                merged_records.append(dict(merged.record))

            if not merged_records:
                self.buffer.drop(key)
                return SessionEndResult(
                    session_id=input_.session_id,
                    write_status="empty",
                )

            record_input = self.build_session_end_record(
                profile=profile,
                merged_records=merged_records,
            )
            await self.embedder.embed_context(record_input.ctx)
            stored = await self.writer.write(record_input)
            self.buffer.drop(key)
            merged_uris = [str(record.get("uri", "")) for record in merged_records]
            self.events.session_ended(
                profile=profile,
                final_uri=stored.uri,
                merged_uris=merged_uris,
                content=record_input.content,
                record=dict(stored.record),
            )
            return SessionEndResult(
                session_id=input_.session_id,
                merged_uris=merged_uris,
                final_uri=stored.uri,
            )

    async def load_merged_records(self, session_id: str) -> list[dict[str, Any]]:
        """Load merged primary records for one session."""
        return await self.storage.filter(
            self.collection_resolver(),
            FilterExpr.all(
                FilterExpr.eq("session_id", session_id),
                FilterExpr.eq("meta.layer", str(SessionRecordLayer.MERGED)),
            ).to_dict(),
            limit=10000,
        )

    def build_session_end_record(
        self,
        *,
        profile: IdentityProfile,
        merged_records: list[dict[str, Any]],
    ) -> PrimaryRecordInput:
        """Build the final session primary record."""
        session_id = profile.session_id
        uri = self.namespace.session_final_uri(session_id, profile=profile)
        parent_uri = self.namespace.session_events_parent(session_id, profile=profile)
        content = "\n\n".join(
            str(record.get("abstract", "") or "") for record in merged_records
        ).strip()
        abstract = smart_truncate(content, 240)
        merged_uris = [str(record.get("uri", "")) for record in merged_records]
        meta = {
            "project_id": profile.project_id,
            "layer": str(SessionRecordLayer.FINAL),
            "session_id": session_id,
            "source_uri": parent_uri,
            "recomposition_stage": "final_full",
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
            user=UserIdentifier(profile.tenant_id, profile.user_id),
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
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            content=content,
        )
