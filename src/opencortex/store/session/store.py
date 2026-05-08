# SPDX-License-Identifier: Apache-2.0
"""Session message store flow."""

from __future__ import annotations

import logging
from typing import Any

from opencortex.core.context import Context, Vectorize
from opencortex.core.identity import IdentityProfile
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import get_identity_profile
from opencortex.retrieve.types import ContextType
from opencortex.storage.cortex_namespace import CortexNamespace
from opencortex.store.common import (
    build_abstract_json,
    memory_object_payload,
    merge_unique_strings,
)
from opencortex.store.embedder import StoreEmbedder
from opencortex.store.event.events import StoreEvents
from opencortex.store.schemas import (
    PrimaryRecordInput,
    SessionMessage,
    SessionMessageInput,
    SessionMessageResult,
)
from opencortex.store.session.buffer import SessionBuffer
from opencortex.store.types import MemoryCategory, SessionRecordLayer
from opencortex.writer.primary_record_writer import PrimaryRecordWriter

logger = logging.getLogger(__name__)


class SessionStore:
    """Store conversation messages as immediate RAG primary records."""

    def __init__(
        self,
        *,
        buffer: SessionBuffer,
        namespace: CortexNamespace,
        embedder: StoreEmbedder,
        writer: PrimaryRecordWriter,
        events: StoreEvents,
    ) -> None:
        self.buffer = buffer
        self.namespace = namespace
        self.embedder = embedder
        self.writer = writer
        self.events = events

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
                record_input = self.build_immediate_record(
                    input_=input_,
                    message=message,
                    msg_index=msg_index,
                    profile=profile,
                )
                await self.embedder.embed_context(record_input.ctx)
                stored = await self.writer.write(record_input)
                self.buffer.append(
                    key,
                    text=record_input.content,
                    record_uri=stored.uri,
                    tool_calls=(
                        input_.tool_calls if message.role == "assistant" else None
                    ),
                )
                written_uris.append(stored.uri)

            self.events.session_turn_stored(
                profile=profile,
                turn_id=input_.turn_id,
                record_uris=written_uris,
                tool_calls=input_.tool_calls,
            )
            merge_requested = self.buffer.should_merge(key)

        return SessionMessageResult(
            turn_id=input_.turn_id,
            written_uris=written_uris,
            merge_requested=merge_requested,
        )

    def build_immediate_record(
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
        entities = merge_unique_strings(meta.get("entities"))
        topics = merge_unique_strings(meta.get("topics"))
        keywords = ", ".join(topics)
        content = self.decorate_message_text(message.content, meta)

        ctx = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=True,
            abstract=content,
            overview="",
            context_type=ContextType.MEMORY,
            category=str(MemoryCategory.EVENTS),
            related_uri=[],
            meta=meta,
            session_id=input_.session_id,
            user=UserIdentifier(profile.tenant_id, profile.user_id),
        )
        ctx.vectorize = Vectorize(self.immediate_embed_text(content))
        abstract_json = build_abstract_json(
            uri=uri,
            context_type=ContextType.MEMORY,
            category=str(MemoryCategory.EVENTS),
            abstract=content,
            overview="",
            content=content,
            entities=entities,
            meta=meta,
            keywords=topics,
            parent_uri=parent_uri,
            session_id=input_.session_id,
        )
        return PrimaryRecordInput(
            ctx=ctx,
            abstract_json=abstract_json,
            object_payload=memory_object_payload(abstract_json, is_leaf=True),
            effective_category=str(MemoryCategory.EVENTS),
            keywords=keywords,
            entities=entities,
            meta=meta,
            context_type=ContextType.MEMORY,
            session_id=input_.session_id,
            tenant_id=profile.tenant_id,
            user_id=profile.user_id,
            content=content,
        )

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
