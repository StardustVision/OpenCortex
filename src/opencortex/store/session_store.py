# SPDX-License-Identifier: Apache-2.0
"""Session message store flow."""

from __future__ import annotations

import logging
from typing import Any

from opencortex.core.context import Context, Vectorize
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import (
    get_collection_name,
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.retrieve.types import ContextType
from opencortex.store.common import (
    build_abstract_json,
    memory_object_payload,
    merge_unique_strings,
)
from opencortex.store.embedder import StoreEmbedder
from opencortex.store.schemas import (
    PrimaryRecordInput,
    SessionMessage,
    SessionMessageInput,
    SessionMessageResult,
)
from opencortex.store.session_buffer import SessionBuffer
from opencortex.store.events import StoreEvents
from opencortex.store.types import MemoryCategory, SessionRecordLayer
from opencortex.storage.cortex_namespace import CortexNamespace
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
        self._buffer = buffer
        self._namespace = namespace
        self._embedder = embedder
        self._writer = writer
        self._events = events

    async def message(self, input_: SessionMessageInput) -> SessionMessageResult:
        """Write one session turn through the direct message store chain."""
        tenant_id, user_id = get_effective_identity()
        key = self._buffer.session_key(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=input_.session_id,
        )

        written_uris: list[str] = []
        merge_requested = False
        async with self._buffer.lock(key):
            self._buffer.touch(key)

            for message in input_.messages:
                msg_index = self._buffer.next_msg_index(key)
                record_input = self.build_immediate_record(
                    input_=input_,
                    message=message,
                    msg_index=msg_index,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
                await self._embedder.embed_context(record_input.ctx)
                stored = await self._writer.write(record_input)
                self._buffer.append(
                    key,
                    text=record_input.content,
                    record_uri=stored.uri,
                    tool_calls=(
                        input_.tool_calls if message.role == "assistant" else None
                    ),
                )
                written_uris.append(stored.uri)

            self._events.session_turn_stored(
                session_id=input_.session_id,
                turn_id=input_.turn_id,
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=get_effective_project_id(),
                record_uris=written_uris,
                tool_calls=input_.tool_calls,
                collection=get_collection_name() or "",
            )
            merge_requested = self._buffer.should_merge(key)

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
        tenant_id: str,
        user_id: str,
    ) -> PrimaryRecordInput:
        """Build the primary-record input for one immediate session message."""
        uri = self._namespace.session_immediate_uri()
        parent_uri = self._namespace.session_events_parent(input_.session_id)
        meta = self._message_meta(
            input_=input_,
            message=message,
            msg_index=msg_index,
        )
        entities = merge_unique_strings(meta.get("entities"))
        topics = merge_unique_strings(meta.get("topics"))
        keywords = ", ".join(topics)
        content = self._decorate_message_text(message.content, meta)

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
            user=UserIdentifier(tenant_id, user_id),
        )
        ctx.vectorize = Vectorize(self._immediate_embed_text(content))
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
            tenant_id=tenant_id,
            user_id=user_id,
            content=content,
        )

    @staticmethod
    def _message_meta(
        *,
        input_: SessionMessageInput,
        message: SessionMessage,
        msg_index: int,
    ) -> dict[str, Any]:
        """Return metadata stored on an immediate session message."""
        meta = dict(message.meta)
        topics = merge_unique_strings(meta.get("topics"))
        if topics:
            meta["topics"] = topics
        meta.update(
            {
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
    def _decorate_message_text(text: str, meta: dict[str, Any]) -> str:
        """Prefix text with the strongest explicit time reference."""
        time_refs = merge_unique_strings(meta.get("time_refs"), meta.get("event_date"))
        if not time_refs:
            return text
        first_ref = time_refs[0]
        if first_ref in text:
            return text
        return f"[{first_ref}] {text}"

    @staticmethod
    def _immediate_embed_text(text: str) -> str:
        """Return embedding text for immediate conversation messages."""
        lowered = text.lower()
        for prefix in ("user:", "assistant:", "system:"):
            if lowered.startswith(prefix):
                return f"[{prefix.rstrip(':')}] {text}"
        return text
