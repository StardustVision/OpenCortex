# SPDX-License-Identifier: Apache-2.0
"""Session end primary record flow."""

from __future__ import annotations

from typing import Any

from qdrant_client import models

from opencortex.core.identity import IdentityProfile, get_identity_profile
from opencortex.storage.namespace import CortexNamespace
from opencortex.store.document_tree import (
    DocumentTreeWriter,
    should_parse_session_tree,
)
from opencortex.store.event.events import StoreEvents
from opencortex.store.schemas import (
    Context,
    PrimaryRecordInput,
    RawPrimaryRecord,
    SessionEndInput,
    SessionEndResult,
)
from opencortex.store.session.buffer import SessionBuffer
from opencortex.store.session.merger import SessionMerger
from opencortex.store.types import ContextType, MemoryCategory, SessionRecordLayer
from opencortex.store.writer.primary_record_writer import PrimaryRecordWriter


class SessionEnder:
    """Close a session by writing the final session primary record."""

    def __init__(
        self,
        *,
        buffer: SessionBuffer,
        merger: SessionMerger,
        namespace: CortexNamespace,
        writer: PrimaryRecordWriter,
        events: StoreEvents,
        vector_store: Any,
        collection_resolver: Any,
        document_tree: DocumentTreeWriter | None = None,
    ) -> None:
        self.buffer = buffer
        self.merger = merger
        self.namespace = namespace
        self.writer = writer
        self.events = events
        self.vector_store = vector_store
        self.collection_resolver = collection_resolver
        self.document_tree = document_tree

    async def end(self, input_: SessionEndInput) -> SessionEndResult:
        """Synchronously close session primary records, then publish events."""
        profile = get_identity_profile(session_id=input_.session_id)
        key = self.buffer.profile_key(profile)
        async with self.buffer.lock(key):
            self.buffer.touch(key, profile)
            self.buffer.freeze_all_chunks(key)
            new_merged_records = []
            while self.buffer.has_pending_merge(key):
                merged = await self.merger.merge_unmerged(
                    key,
                    profile=profile,
                )
                if merged is not None:
                    new_merged_records.append(dict(merged.record))
            merged_records = await self.load_merged_records(input_.session_id)
            known_uris = {str(record.get("uri", "")) for record in merged_records}
            for record in new_merged_records:
                uri = str(record.get("uri", ""))
                if uri and uri not in known_uris:
                    merged_records.append(record)
                    known_uris.add(uri)

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
            stored = await self.writer.write(record_input)
            if self.document_tree is not None and should_parse_session_tree(
                record_input.content
            ):
                await self.document_tree.write_children(
                    root_input=record_input,
                    source_format="markdown",
                    chunk_role="session_final_section",
                )
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
        return await self.vector_store.filter(
            self.collection_resolver(),
            models.Filter(
                must=[
                    models.FieldCondition(
                        key="session_id",
                        match=models.MatchValue(value=session_id),
                    ),
                    models.FieldCondition(
                        key="meta.layer",
                        match=models.MatchValue(
                            value=str(SessionRecordLayer.MERGED),
                        ),
                    ),
                    models.FieldCondition(
                        key="retrieval_surface",
                        match=models.MatchValue(value="l0_object"),
                    ),
                ]
            ),
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
            str(record.get("content") or record.get("abstract") or "")
            for record in merged_records
        )
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
            context_type=ContextType.MEMORY,
            category=str(MemoryCategory.EVENTS),
            meta=meta,
            session_id=session_id,
            profile=profile,
        )
        raw_record = RawPrimaryRecord.from_context(
            ctx=ctx,
            content=content,
            effective_category=str(MemoryCategory.EVENTS),
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            project_id=profile.project_id,
            session_id=session_id,
            meta=meta,
        )
        return PrimaryRecordInput(
            ctx=ctx,
            payload=raw_record.model_dump(mode="json"),
            effective_category=raw_record.category,
            meta=meta,
            context_type=ContextType.MEMORY,
            session_id=session_id,
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            content=content,
        )
