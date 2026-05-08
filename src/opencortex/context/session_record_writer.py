# SPDX-License-Identifier: Apache-2.0
"""Session record write boundary.

Session writes follow the same storage shape as normal store writes:
primary record first, then secondary indexes, then blob/side effects.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

from opencortex.core.context import Context, Vectorize
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import get_effective_project_id
from opencortex.services.derivation_service import _merge_unique_strings
from opencortex.store.event.events import (
    MemoryStoredEvent,
    SessionTurnStoredEvent,
)
from opencortex.utils.uri import CortexURI

if TYPE_CHECKING:
    from opencortex.context.manager import ContextManager

logger = logging.getLogger(__name__)

_IMMEDIATE_EMBED_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class SessionRecordDraft:
    """Session record input assembled before persistence."""

    ctx: Context
    content: str
    abstract_json: Dict[str, Any]
    object_payload: Dict[str, Any]
    keywords: str
    entities: List[str]
    meta: Dict[str, Any]
    tenant_id: str
    user_id: str
    sparse_vector: Optional[Any] = None


class SessionRecordWriter:
    """Writes conversation records behind commit/end lifecycle code."""

    def __init__(self, manager: "ContextManager") -> None:
        """Create a session writer bound to one context manager."""
        self._manager = manager

    async def write_immediate_message(
        self,
        *,
        session_id: str,
        msg_index: int,
        text: str,
        tenant_id: str,
        user_id: str,
        tool_calls: Optional[list] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Write a single immediate conversation message."""
        draft = await self._build_immediate_draft(
            session_id=session_id,
            msg_index=msg_index,
            text=text,
            tenant_id=tenant_id,
            user_id=user_id,
            tool_calls=tool_calls,
            meta=meta,
        )
        record = self.build_primary_record(draft)
        await self.upsert_primary_record(record)
        self.publish_primary_saved(
            record=record,
            draft=draft,
            publish_event=True,
        )
        return str(record["uri"])

    async def add_session_record(
        self,
        *,
        uri: str,
        abstract: str,
        content: str,
        category: str,
        context_type: str,
        session_id: str,
        tenant_id: str,
        user_id: str,
        is_leaf: bool,
        meta: Dict[str, Any],
        overview: str = "",
        defer_derive: bool = False,
    ) -> Context:
        """Persist a recomposed session record without crossing the facade."""
        tokens_for_identity = self._set_identity(tenant_id, user_id)
        try:
            draft = await self._build_session_draft(
                uri=uri,
                abstract=abstract,
                content=content,
                category=category,
                context_type=context_type,
                overview=overview,
                is_leaf=is_leaf,
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                meta=meta,
                defer_derive=defer_derive,
            )
            record = self.build_primary_record(draft)
            await self.upsert_primary_record(record)
            self.publish_primary_saved(
                record=record,
                draft=draft,
                publish_event=True,
            )
            draft.ctx.meta["dedup_action"] = "created"
            return draft.ctx
        finally:
            self._reset_identity(tokens_for_identity)

    def build_primary_record(self, draft: SessionRecordDraft) -> Dict[str, Any]:
        """Build the Qdrant primary payload for one session record."""
        record = draft.ctx.to_dict()
        if draft.ctx.vector:
            record["vector"] = draft.ctx.vector
        if draft.sparse_vector:
            record["sparse_vector"] = draft.sparse_vector

        record["scope"] = "private"
        record["source_user_id"] = draft.user_id
        record["source_tenant_id"] = draft.tenant_id
        record["keywords"] = draft.keywords
        record["entities"] = draft.entities
        record["session_id"] = str(draft.ctx.session_id or "")
        record["project_id"] = get_effective_project_id()
        record["ttl_expires_at"] = self._ttl_for_record(record=record, meta=draft.meta)
        record["speaker"] = str(draft.meta.get("speaker", "") or "")
        record["event_date"] = draft.meta.get("event_date")
        record.update(draft.object_payload)
        record["abstract_json"] = draft.abstract_json
        self._populate_flattened_source_fields(record, draft.meta)
        return record

    async def upsert_primary_record(self, record: Dict[str, Any]) -> None:
        """Write the session primary record to the active collection."""
        record_id = await self._orchestrator._storage.upsert(
            self._orchestrator._get_collection(),
            record,
        )
        record["id"] = record_id

    def publish_primary_saved(
        self,
        *,
        record: Dict[str, Any],
        draft: SessionRecordDraft,
        publish_event: bool,
    ) -> None:
        """Publish post-primary-write event without blocking the caller."""
        if publish_event and draft.meta.get("layer") == "immediate":
            self._publish_session_turn_stored(record, content=draft.content)
        elif publish_event:
            self._publish_session_record_stored(record, content=draft.content)

    async def _build_immediate_draft(
        self,
        *,
        session_id: str,
        msg_index: int,
        text: str,
        tenant_id: str,
        user_id: str,
        tool_calls: Optional[list],
        meta: Optional[Dict[str, Any]],
    ) -> SessionRecordDraft:
        """Assemble and embed an immediate-message primary record draft."""
        uri = CortexURI.build_private(
            tenant_id,
            user_id,
            "memories",
            "events",
            uuid4().hex,
        )
        parent_uri = CortexURI.build_private(
            tenant_id,
            user_id,
            "memories",
            "events",
            session_id,
        )
        record_meta = self._immediate_meta(
            session_id=session_id,
            msg_index=msg_index,
            tool_calls=tool_calls,
            meta=meta,
        )
        entities = _merge_unique_strings(record_meta.get("entities"))
        topics = _merge_unique_strings(record_meta.get("topics"))
        keywords = ", ".join(topics)

        ctx = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=True,
            abstract=text,
            overview="",
            context_type="memory",
            category="events",
            meta=record_meta,
            session_id=session_id,
            user=UserIdentifier(tenant_id, user_id),
        )
        ctx.vectorize = Vectorize(self._immediate_embed_text(text))
        sparse_vector = await self._embed_context(ctx)

        abstract_json = self._orchestrator._build_abstract_json(
            uri=uri,
            context_type="memory",
            category="events",
            abstract=text,
            overview="",
            content=text,
            entities=entities,
            meta=record_meta,
            keywords=topics,
            parent_uri=parent_uri,
            session_id=session_id,
        )
        object_payload = self._orchestrator._memory_object_payload(
            abstract_json,
            is_leaf=True,
        )
        return SessionRecordDraft(
            ctx=ctx,
            content=text,
            abstract_json=abstract_json,
            object_payload=object_payload,
            keywords=keywords,
            entities=entities,
            meta=record_meta,
            tenant_id=tenant_id,
            user_id=user_id,
            sparse_vector=sparse_vector,
        )

    async def _build_session_draft(
        self,
        *,
        uri: str,
        abstract: str,
        content: str,
        category: str,
        context_type: str,
        overview: str,
        is_leaf: bool,
        session_id: str,
        tenant_id: str,
        user_id: str,
        meta: Dict[str, Any],
        defer_derive: bool,
    ) -> SessionRecordDraft:
        """Assemble and embed a merged/end-phase session record draft."""
        write_engine = self._memory_writer
        target = await write_engine._context_builder.resolve_target(
            abstract=abstract,
            category=category,
            context_type=context_type,
            meta=meta,
            parent_uri=None,
            uri=uri,
        )
        derive_result = await write_engine._write_derive_service.derive_for_write(
            abstract=abstract,
            overview=overview,
            content=content,
            is_leaf=is_leaf,
            defer_derive=defer_derive,
        )
        assembled = write_engine._context_builder.assemble_context(
            target=target,
            abstract=derive_result.abstract,
            overview=derive_result.overview,
            content=content,
            category=category,
            context_type=context_type,
            is_leaf=is_leaf,
            related_uri=[],
            session_id=session_id,
            embed_text="",
            layers=derive_result.layers,
        )
        embed_result = await write_engine._write_embed_service.embed_for_write(
            assembled.ctx
        )
        return SessionRecordDraft(
            ctx=assembled.ctx,
            content=content,
            abstract_json=assembled.abstract_json,
            object_payload=assembled.object_payload,
            keywords=assembled.keywords,
            entities=assembled.entities,
            meta=assembled.meta,
            tenant_id=tenant_id,
            user_id=user_id,
            sparse_vector=embed_result.sparse_vector,
        )

    @staticmethod
    def _immediate_meta(
        *,
        session_id: str,
        msg_index: int,
        tool_calls: Optional[list],
        meta: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Return metadata for one immediate conversation message."""
        record_meta = dict(meta or {})
        explicit_topics = _merge_unique_strings(record_meta.get("topics"))
        if explicit_topics:
            record_meta["topics"] = explicit_topics
        record_meta.update(
            {
                "layer": "immediate",
                "msg_index": msg_index,
                "session_id": session_id,
                "tool_calls": tool_calls or [],
            }
        )
        return record_meta

    def _immediate_embed_text(self, text: str) -> str:
        """Return embedding text for immediate conversation messages."""
        if not self._orchestrator._config.context_flattening_enabled:
            return text
        for prefix in ("user:", "assistant:", "system:"):
            if text.lower().startswith(prefix):
                return f"[{prefix.rstrip(':')}] {text}"
        return text

    async def _embed_context(self, ctx: Context) -> Optional[Any]:
        """Embed a session context and attach its dense vector."""
        embedder = self._orchestrator._embedder
        if not embedder:
            return None

        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None, embedder.embed, ctx.get_vectorization_text()
                ),
                timeout=_IMMEDIATE_EMBED_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            fallback_embedder = self._immediate_fallback_embedder(exc)
            if fallback_embedder is None:
                raise
            logger.warning(
                "[SessionRecordWriter] Immediate remote embedding failed; "
                "retrying local fallback model=%s exc_type=%s exc=%r",
                getattr(fallback_embedder, "model_name", "local-fallback"),
                type(exc).__name__,
                exc,
            )
            try:
                result = await loop.run_in_executor(
                    None,
                    fallback_embedder.embed,
                    ctx.get_vectorization_text(),
                )
            except Exception as fallback_exc:
                logger.warning(
                    "[SessionRecordWriter] Immediate local fallback embedding "
                    "failed model=%s exc_type=%s exc=%r",
                    getattr(fallback_embedder, "model_name", "local-fallback"),
                    type(fallback_exc).__name__,
                    fallback_exc,
                )
                raise exc from fallback_exc

        ctx.vector = result.dense_vector
        return result.sparse_vector if result.sparse_vector else None

    def _immediate_fallback_embedder(self, exc: Exception) -> Optional[Any]:
        """Return local fallback embedder for retryable immediate failures."""
        memory = self._orchestrator
        if (memory._config.embedding_provider or "").strip().lower() != "openai":
            return None
        if not memory._is_retryable_immediate_embed_exception(exc):
            return None
        return memory._get_immediate_fallback_embedder()

    def _ttl_for_record(self, *, record: Dict[str, Any], meta: Dict[str, Any]) -> str:
        """Return the TTL for one session-written primary record."""
        if meta.get("layer") == "immediate":
            return self._orchestrator._ttl_from_hours(
                self._orchestrator._config.immediate_event_ttl_hours
            )
        if (
            record.get("context_type") == "memory"
            and record.get("category") == "events"
            and meta.get("layer") == "merged"
        ):
            return self._orchestrator._ttl_from_hours(
                self._orchestrator._config.merged_event_ttl_hours
            )
        return ""

    @staticmethod
    def _populate_flattened_source_fields(
        record: Dict[str, Any],
        meta: Dict[str, Any],
    ) -> None:
        """Copy document/conversation enrichment fields to top level."""
        record["source_doc_id"] = meta.get("source_doc_id", "")
        record["source_doc_title"] = meta.get("source_doc_title", "")
        record["source_section_path"] = meta.get("source_section_path", "")
        record["chunk_role"] = meta.get("chunk_role", "")

    def _publish_session_record_stored(
        self,
        record: Dict[str, Any],
        *,
        content: str,
    ) -> None:
        """Reserved event hook for session-specific write events."""
        memory_events = getattr(self._orchestrator, "_memory_events", None)
        if memory_events is None:
            return
        memory_events.publish_nowait(
            MemoryStoredEvent(
                uri=str(record.get("uri", "")),
                record_id=str(record.get("id", "")),
                tenant_id=str(record.get("source_tenant_id", "")),
                user_id=str(record.get("source_user_id", "")),
                project_id=str(record.get("project_id", "")),
                context_type=str(record.get("context_type", "")),
                category=str(record.get("category", "")),
                content=content,
                record=dict(record),
            )
        )

    def _publish_session_turn_stored(
        self,
        record: Dict[str, Any],
        *,
        content: str,
    ) -> None:
        """Publish immediate-message primary-write event."""
        memory_events = getattr(self._orchestrator, "_memory_events", None)
        if memory_events is None:
            return
        memory_events.publish_nowait(
            SessionTurnStoredEvent(
                session_id=str(record.get("session_id", "")),
                tenant_id=str(record.get("source_tenant_id", "")),
                user_id=str(record.get("source_user_id", "")),
                project_id=str(record.get("project_id", "")),
                turn_id=str((record.get("meta") or {}).get("turn_id") or ""),
                record_uris=[str(record.get("uri", ""))],
                tool_calls=list((record.get("meta") or {}).get("tool_calls") or []),
            )
        )

    @property
    def _orchestrator(self) -> Any:
        return self._manager._orchestrator

    @property
    def _memory_writer(self) -> Any:
        return self._orchestrator._memory_service._memory_writer

    @staticmethod
    def _set_identity(tenant_id: str, user_id: str) -> Any:
        from opencortex.http.request_context import set_request_identity

        return set_request_identity(tenant_id, user_id)

    @staticmethod
    def _reset_identity(tokens: Any) -> None:
        from opencortex.http.request_context import reset_request_identity

        reset_request_identity(tokens)
