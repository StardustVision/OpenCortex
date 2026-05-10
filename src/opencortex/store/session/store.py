# SPDX-License-Identifier: Apache-2.0
"""Session message store flow."""

from __future__ import annotations

from typing import Any

import structlog

from opencortex.core.identity import IdentityProfile, get_identity_profile
from opencortex.storage.namespace import CortexNamespace
from opencortex.store.common import (
    build_abstract_json,
    memory_object_payload,
    merge_unique_strings,
)
from opencortex.store.event.events import StoreEvents
from opencortex.store.schemas import (
    Context,
    PrimaryRecordInput,
    RawPrimaryRecord,
    SessionMessage,
    SessionMessageInput,
    SessionMessageResult,
    Vectorize,
    primary_ttl,
)
from opencortex.store.session.buffer import SessionBuffer
from opencortex.store.types import ContextType, MemoryCategory, SessionRecordLayer
from opencortex.store.writer.primary_record_writer import PrimaryRecordWriter

logger = structlog.get_logger(__name__)


class SessionStore:
    """Store conversation messages as immediate RAG primary records."""

    def __init__(
        self,
        *,
        buffer: SessionBuffer,
        namespace: CortexNamespace,
        embedder: Any,
        writer: PrimaryRecordWriter,
        events: StoreEvents,
        config: Any,
        ttl_from_hours: Any,
    ) -> None:
        self.buffer = buffer
        self.namespace = namespace
        self.embedder = embedder
        self.writer = writer
        self.events = events
        self.config = config
        self.ttl_from_hours = ttl_from_hours

    async def message(self, input_: SessionMessageInput) -> SessionMessageResult:
        """Write one session turn through the direct message store chain."""
        profile = get_identity_profile(session_id=input_.session_id)
        key = self.buffer.profile_key(profile)

        written_uris: list[str] = []
        merge_requested = False
        async with self.buffer.lock(key):
            self.buffer.touch(key, profile)

            for message in input_.messages:
                msg_index = self.buffer.next_msg_index(key)
                record_input = await self.build_immediate_record(
                    input_=input_,
                    message=message,
                    msg_index=msg_index,
                    profile=profile,
                )
                stored = await self.writer.write(record_input)
                self.events.memory_stored(record_input, stored)
                self.buffer.append(
                    key,
                    text=record_input.content,
                    record_uri=stored.uri,
                    tool_calls=(
                        input_.tool_calls if message.role == "assistant" else None
                    ),
                )
                written_uris.append(stored.uri)
                if self.buffer.freeze_ready_chunks(key):
                    merge_requested = True

            self.events.session_turn_stored(
                profile=profile,
                turn_id=input_.turn_id,
                record_uris=written_uris,
                tool_calls=input_.tool_calls,
            )
            merge_requested = merge_requested or self.buffer.should_merge(key)

        return SessionMessageResult(
            turn_id=input_.turn_id,
            written_uris=written_uris,
            merge_requested=merge_requested,
        )

    async def build_immediate_record(
        self,
        *,
        input_: SessionMessageInput,
        message: SessionMessage,
        msg_index: int,
        profile: IdentityProfile,
    ) -> PrimaryRecordInput:
        """Build the primary-record input for one immediate session message."""
        uri = self.namespace.session_immediate_uri(profile=profile)
        parent_uri = self.namespace.session_events_parent(
            input_.session_id,
            profile=profile,
        )
        meta = self.message_meta(
            input_=input_,
            message=message,
            msg_index=msg_index,
            profile=profile,
        )
        content = self.decorate_message_text(message.content, meta)

        ctx = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=True,
            context_type=ContextType.MEMORY,
            category=str(MemoryCategory.EVENTS),
            related_uri=[],
            meta=meta,
            session_id=input_.session_id,
            profile=profile,
        )
        raw_record = RawPrimaryRecord.from_context(
            ctx=ctx,
            content=content,
            effective_category=str(MemoryCategory.EVENTS),
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            project_id=profile.project_id,
            session_id=input_.session_id,
            meta=meta,
            ttl_expires_at=primary_ttl(
                config=self.config,
                ttl_from_hours=self.ttl_from_hours,
                context_type=ContextType.MEMORY,
                category=str(MemoryCategory.EVENTS),
                layer=str(SessionRecordLayer.IMMEDIATE),
            ),
        )
        await self.prepare_immediate_ready_payload(raw_record, ctx, meta)
        return PrimaryRecordInput(
            ctx=ctx,
            payload=raw_record.model_dump(mode="json"),
            effective_category=raw_record.category,
            meta=meta,
            context_type=ContextType.MEMORY,
            session_id=input_.session_id,
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            content=content,
        )

    async def prepare_immediate_ready_payload(
        self,
        raw_record: RawPrimaryRecord,
        ctx: Context,
        meta: dict[str, Any],
    ) -> None:
        """Make an immediate message synchronously retrieval-ready."""
        entities = merge_unique_strings(meta.get("entities"))
        keywords_list = merge_unique_strings(meta.get("topics"))
        keywords = ", ".join(keywords_list)
        abstract_json = build_abstract_json(
            uri=raw_record.uri,
            context_type=str(ContextType.MEMORY),
            category=str(MemoryCategory.EVENTS),
            abstract=raw_record.content,
            overview="",
            content=raw_record.content,
            entities=entities,
            meta=meta,
            keywords=keywords_list,
            parent_uri=raw_record.parent_uri,
            session_id=raw_record.session_id,
        )
        abstract_json["fact_points"] = [raw_record.content]
        raw_record.abstract = raw_record.content
        raw_record.overview = ""
        raw_record.entities = entities
        raw_record.keywords = keywords
        raw_record.abstract_json = abstract_json
        raw_record.retrieval_surface = "l0_object"
        raw_record.retrieval_ready = True
        raw_record.derive_status = "ready"
        raw_record.meta = meta
        for key, value in memory_object_payload(abstract_json, is_leaf=True).items():
            setattr(raw_record, key, value)
        ctx.abstract = raw_record.abstract
        ctx.overview = raw_record.overview
        ctx.vectorize = Vectorize(self.immediate_embed_text(raw_record.content))
        embedding = await self.embedder.embed_context(ctx)
        if not ctx.vector:
            raise ValueError("Immediate embedding returned no dense vector")
        raw_record.vector = ctx.vector
        if embedding.sparse_vector:
            raw_record.sparse_vector = embedding.sparse_vector

    @staticmethod
    def message_meta(
        *,
        input_: SessionMessageInput,
        message: SessionMessage,
        msg_index: int,
        profile: IdentityProfile,
    ) -> dict[str, Any]:
        """Return metadata stored on an immediate session message."""
        meta = dict(message.meta)
        topics = merge_unique_strings(meta.get("topics"))
        if topics:
            meta["topics"] = topics
        meta.update(
            {
                "project_id": profile.project_id,
                "layer": str(SessionRecordLayer.IMMEDIATE),
                "session_id": input_.session_id,
                "turn_id": input_.turn_id,
                "msg_index": msg_index,
                "role": message.role,
                "tool_calls": input_.tool_calls if message.role == "assistant" else [],
            }
        )
        return meta

    @staticmethod
    def decorate_message_text(text: str, meta: dict[str, Any]) -> str:
        """Prefix text with the strongest explicit time reference."""
        time_refs = merge_unique_strings(meta.get("time_refs"), meta.get("event_date"))
        if not time_refs:
            return text
        first_ref = time_refs[0]
        if first_ref in text:
            return text
        return f"[{first_ref}] {text}"

    @staticmethod
    def immediate_embed_text(text: str) -> str:
        """Return embedding text for immediate conversation messages."""
        lowered = text.lower()
        for prefix in ("user:", "assistant:", "system:"):
            if lowered.startswith(prefix):
                return f"[{prefix.rstrip(':')}] {text}"
        return text
