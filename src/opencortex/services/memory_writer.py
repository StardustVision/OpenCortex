# SPDX-License-Identifier: Apache-2.0
"""Memory writer for OpenCortex.

This module owns add/update/remove/document ingest/batch write behavior through
explicit dependencies. ``MemoryService`` may delegate here for compatibility,
but the writer does not depend on that facade.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from opencortex.core.context import Context
from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.retrieve.types import ContextType
from opencortex.store.event.events import MemoryStoredEvent
from opencortex.utils.uri import CortexURI

if TYPE_CHECKING:
    from opencortex.services.memory_directory_record_service import (
        MemoryDirectoryRecordService,
    )
    from opencortex.services.memory_document_write_service import (
        MemoryDocumentWriteService,
    )
    from opencortex.services.memory_mutation_service import MemoryMutationService
    from opencortex.services.memory_write_context_builder import (
        MemoryWriteContextBuilder,
    )
    from opencortex.services.memory_write_dedup_service import MemoryWriteDedupService
    from opencortex.services.memory_write_derive_service import MemoryWriteDeriveService
    from opencortex.services.memory_write_embed_service import MemoryWriteEmbedService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryWriterDependencies:
    """Explicit subsystem bundle used by the normal store write path."""

    config: Any
    storage: Any
    fs: Any
    embedder: Any
    memory_events: Any
    entity_index: Any
    memory_record_service: Any
    derivation_service: Any
    session_lifecycle_service: Any
    ensure_init: Any
    get_collection: Any
    feedback: Any
    llm_completion: Any = None
    parser_registry: Any = None
    set_parser_registry: Any = None
    derive_queue: Any = None
    inflight_derive_uris: Any = None


class MemoryWriter:
    """Own memory write/mutation behavior through explicit collaborators."""

    def __init__(
        self,
        dependencies: MemoryWriterDependencies,
    ) -> None:
        self._deps = dependencies
        self._parser_registry = dependencies.parser_registry

    def set_parser_registry(self, parser_registry: Any) -> None:
        """Bind the parser registry owned by this writer."""
        self._parser_registry = parser_registry
        if self._deps.set_parser_registry is not None:
            self._deps.set_parser_registry(parser_registry)

    @property
    def _config(self) -> Any:
        """Cortex configuration for write-path helpers."""
        return self._deps.config

    @property
    def _storage(self) -> Any:
        """Vector storage owned by the memory facade."""
        return self._deps.storage

    @property
    def _fs(self) -> Any:
        """CortexFS instance owned by the memory facade."""
        return self._deps.fs

    @property
    def _embedder(self) -> Any:
        """Embedder used by normal write-path helpers."""
        return self._deps.embedder

    @property
    def _memory_events(self) -> Any:
        """Optional lifecycle event bus for write-path notifications."""
        return self._deps.memory_events

    @property
    def _entity_index(self) -> Any:
        """Optional entity index for write-path synchronization."""
        return self._deps.entity_index

    @property
    def _llm_completion(self) -> Any:
        """Optional LLM callable used for document summary derivation."""
        return self._deps.llm_completion

    @property
    def _derive_queue(self) -> Any:
        """Background document-derive queue used by resource ingestion."""
        return self._deps.derive_queue

    @property
    def _inflight_derive_uris(self) -> Any:
        """Set of document parent URIs currently queued for derive."""
        return self._deps.inflight_derive_uris

    def _ensure_init(self) -> None:
        """Require the parent memory facade to be initialized."""
        self._deps.ensure_init()

    def _get_collection(self) -> str:
        """Return the active vector-store collection."""
        return self._deps.get_collection()

    def _auto_uri(self, context_type: str, category: str, abstract: str = "") -> str:
        """Generate a memory URI through the record service boundary."""
        return self._deps.memory_record_service._auto_uri(
            context_type=context_type,
            category=category,
            abstract=abstract,
        )

    async def _resolve_unique_uri(self, uri: str) -> str:
        """Resolve one URI to a unique value."""
        return await self._deps.memory_record_service._resolve_unique_uri(uri)

    async def _get_record_by_uri(self, uri: str) -> Optional[Dict[str, Any]]:
        """Load one record by URI through the session/record boundary."""
        return await self._deps.session_lifecycle_service._get_record_by_uri(uri)

    def _derive_parent_uri(self, uri: str) -> str:
        """Derive the parent URI for a memory URI."""
        return self._deps.memory_record_service._derive_parent_uri(uri)

    def _extract_category_from_uri(self, uri: str) -> str:
        """Extract the memory category from a URI."""
        return self._deps.memory_record_service._extract_category_from_uri(uri)

    def _build_abstract_json(
        self,
        *,
        uri: str,
        context_type: str,
        category: str,
        abstract: str,
        overview: str,
        content: str,
        entities: List[str],
        meta: Optional[Dict[str, Any]],
        keywords: Optional[List[str]] = None,
        parent_uri: str,
        session_id: Optional[str],
    ) -> Dict[str, Any]:
        """Build the canonical abstract payload for a write record."""
        return self._deps.memory_record_service._build_abstract_json(
            uri=uri,
            context_type=context_type,
            category=category,
            abstract=abstract,
            overview=overview,
            content=content,
            entities=entities,
            meta=meta,
            keywords=keywords,
            parent_uri=parent_uri,
            session_id=session_id or "",
        )

    def _memory_object_payload(
        self,
        abstract_json: Dict[str, Any],
        *,
        is_leaf: bool,
    ) -> Dict[str, Any]:
        """Project abstract payload into flat memory object fields."""
        return self._deps.memory_record_service._memory_object_payload(
            abstract_json, is_leaf=is_leaf
        )

    async def _derive_layers(
        self,
        *,
        user_abstract: str,
        content: str,
        user_overview: str,
    ) -> Dict[str, Any]:
        """Derive memory layers for write-path content."""
        return await self._deps.derivation_service._derive_layers(
            user_abstract=user_abstract,
            content=content,
            user_overview=user_overview,
        )

    def _fallback_overview_from_content(
        self,
        *,
        user_overview: str,
        content: str,
    ) -> str:
        """Build a deterministic fallback overview for deferred derive."""
        return self._deps.derivation_service._fallback_overview_from_content(
            user_overview=user_overview,
            content=content,
        )

    def _derive_abstract_from_overview(
        self,
        *,
        user_abstract: str,
        overview: str,
        content: str,
    ) -> str:
        """Build a deterministic fallback abstract for deferred derive."""
        return self._deps.derivation_service._derive_abstract_from_overview(
            user_abstract=user_abstract,
            overview=overview,
            content=content,
        )

    def _ttl_from_hours(self, hours: int) -> str:
        """Return the TTL string for a write-path record."""
        return self._deps.memory_record_service._ttl_from_hours(hours)

    async def _sync_anchor_projection_records(
        self,
        *,
        source_record: Dict[str, Any],
        abstract_json: Dict[str, Any],
    ) -> None:
        """Synchronize derived anchor/fact projection records."""
        await self._deps.memory_record_service._sync_anchor_projection_records(
            source_record=source_record,
            abstract_json=abstract_json,
        )

    # =========================================================================
    # CRUD (U2 of plan 010)
    # =========================================================================

    async def update(
        self,
        uri: str,
        abstract: Optional[str] = None,
        content: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        overview: Optional[str] = None,
    ) -> bool:
        """Update an existing context.

        Re-embeds if abstract changes, updates vector DB and filesystem.

        Args:
            uri: URI of the context to update.
            abstract: New abstract (re-embeds if changed).
            content: New full content.
            meta: Metadata fields to merge.
            overview: New L1 overview. When provided together with
                ``abstract``, the ``_derive_layers`` fast-path is used
                (no extra LLM call).

        Returns:
            ``True`` if the context was found and updated, ``False`` if
            no record existed at ``uri``.
        """
        return await self._mutation_service.update(
            uri=uri,
            abstract=abstract,
            content=content,
            meta=meta,
            overview=overview,
        )

    async def remove(self, uri: str, recursive: bool = True) -> int:
        """Remove a context from both vector DB and filesystem.

        Args:
            uri: URI of the context to remove.
            recursive: If True, removes all descendants (for directories).

        Returns:
            Number of records removed from the vector DB. Filesystem
            removal failures are logged but do not affect the count
            or raise.
        """
        return await self._mutation_service.remove(uri, recursive=recursive)

    async def add(
        self,
        abstract: str,
        content: str = "",
        overview: str = "",
        category: str = "",
        parent_uri: Optional[str] = None,
        uri: Optional[str] = None,
        context_type: Optional[str] = None,
        is_leaf: bool = True,
        meta: Optional[Dict[str, Any]] = None,
        related_uri: Optional[List[str]] = None,
        session_id: Optional[str] = None,
        dedup: bool = False,
        dedup_threshold: float = 0.82,
        embed_text: str = "",
        defer_derive: bool = False,
        force_primary: bool = False,
    ) -> Context:
        """Add a new context and persist it to vector DB + filesystem.

        Args:
            abstract: Short summary used as L0 and for embedding.
            content: Full text stored as L2. When present and
                ``is_leaf`` is True, LLM-derives overview/keywords.
            overview: Optional L1 overview override.
            category: Dot-separated category path (e.g. ``"documents"``).
            parent_uri: URI of the parent directory node.
            uri: Explicit URI; auto-generated when omitted.
            context_type: One of ``memory``, ``resource``, ``skill``,
                ``staging``.
            is_leaf: False for directory nodes.
            meta: Arbitrary metadata dict merged into the record.
            related_uri: URIs of related contexts.
            session_id: Session this record belongs to.
            dedup: Accepted for API compatibility; merge runs out of band.
            dedup_threshold: Accepted for API compatibility.
            embed_text: Override text used for embedding (takes
                priority over abstract + keywords).
            defer_derive: Skip LLM derivation; use truncation as
                placeholder.
            force_primary: Internal flag used by document ingestion to write
                resource chunks without re-entering document routing.

        Returns:
            The created ``Context`` with ``meta["dedup_action"]`` set
            to ``"created"``.
        """
        self._ensure_init()

        if (
            not force_primary
            and context_type == ContextType.RESOURCE
            and content
            and is_leaf
        ):
            return await self._document_write_service._add_document(
                content=content,
                abstract=abstract,
                overview=overview,
                category=category,
                parent_uri=parent_uri,
                context_type=ContextType.RESOURCE,
                meta=meta,
                session_id=session_id,
                source_path=(meta or {}).get("source_path", ""),
            )

        add_started = asyncio.get_running_loop().time()
        embed_ms = 0
        upsert_ms = 0
        fs_write_ms = 0

        target = await self._context_builder.resolve_target(
            abstract=abstract,
            category=category,
            context_type=context_type,
            meta=meta,
            parent_uri=parent_uri,
            uri=uri,
        )
        uri = target.uri
        _ = target.parent_uri

        derive_result = await self._write_derive_service.derive_for_write(
            abstract=abstract,
            overview=overview,
            content=content,
            is_leaf=is_leaf,
            defer_derive=defer_derive,
        )
        abstract = derive_result.abstract
        overview = derive_result.overview
        layers = derive_result.layers
        derive_layers_ms = derive_result.derive_layers_ms

        # Read effective identity for downstream dedup and persistence.
        tid, uid = get_effective_identity()
        assembled = self._context_builder.assemble_context(
            target=target,
            abstract=abstract,
            overview=overview,
            content=content,
            category=category,
            context_type=context_type,
            is_leaf=is_leaf,
            related_uri=related_uri or [],
            session_id=session_id,
            embed_text=embed_text,
            layers=layers,
        )
        ctx = assembled.ctx
        abstract = assembled.abstract
        overview = assembled.overview
        keywords = assembled.keywords
        entities = assembled.entities
        meta = assembled.meta
        effective_category = assembled.effective_category
        abstract_json = assembled.abstract_json
        object_payload = assembled.object_payload

        embed_result = await self._write_embed_service.embed_for_write(ctx)
        embed_ms = embed_result.embed_ms

        record = self.build_primary_record(
            ctx=ctx,
            abstract_json=abstract_json,
            object_payload=object_payload,
            effective_category=effective_category,
            keywords=keywords,
            entities=entities,
            meta=meta,
            context_type=context_type,
            session_id=session_id,
            tenant_id=tid,
            user_id=uid,
            sparse_vector=embed_result.sparse_vector,
        )
        upsert_ms = await self.upsert_primary_record(record)
        self.publish_memory_stored(
            record=record,
            ctx=ctx,
            content=content,
            tenant_id=tid,
            user_id=uid,
            context_type=context_type,
            effective_category=effective_category,
        )
        fs_write_ms = 0  # Non-blocking

        ctx.meta["dedup_action"] = "created"
        total_ms = int((asyncio.get_running_loop().time() - add_started) * 1000)
        logger.info(
            "[MemoryService] add tenant=%s user=%s uri=%s dedup_action=created "
            "timing_ms(total=%d derive_layers=%d embed=%d upsert=%d fs_write=%d)",
            tid,
            uid,
            uri,
            total_ms,
            derive_layers_ms,
            embed_ms,
            upsert_ms,
            fs_write_ms,
        )
        return ctx

    # ------------------------------------------------------------------
    # Write-time dedup helpers
    # ------------------------------------------------------------------

    def build_primary_record(
        self,
        *,
        ctx: Context,
        abstract_json: Dict[str, Any],
        object_payload: Dict[str, Any],
        effective_category: str,
        keywords: str,
        entities: List[str],
        meta: Dict[str, Any],
        context_type: Optional[str],
        session_id: Optional[str],
        tenant_id: str,
        user_id: str,
        sparse_vector: Optional[Any],
    ) -> Dict[str, Any]:
        """Build the primary memory record payload."""
        record = ctx.to_dict()
        if ctx.vector:
            record["vector"] = ctx.vector
        if sparse_vector:
            record["sparse_vector"] = sparse_vector

        record["scope"] = "private" if CortexURI(ctx.uri).is_private else "shared"
        record["category"] = effective_category
        record["source_user_id"] = user_id
        record["session_id"] = session_id or ""
        record["ttl_expires_at"] = self._ttl_for_store_record(
            context_type=context_type,
            effective_category=effective_category,
            meta=meta,
        )
        record["project_id"] = get_effective_project_id()
        record["source_tenant_id"] = tenant_id
        record["keywords"] = keywords
        record["entities"] = entities
        record.update(object_payload)
        record["abstract_json"] = abstract_json
        self._populate_store_source_fields(record, meta)
        return record

    async def upsert_primary_record(self, record: Dict[str, Any]) -> int:
        """Upsert the primary memory record."""
        upsert_started = asyncio.get_running_loop().time()
        await self._storage.upsert(self._get_collection(), record)
        return int((asyncio.get_running_loop().time() - upsert_started) * 1000)

    def publish_memory_stored(
        self,
        *,
        record: Dict[str, Any],
        ctx: Context,
        content: str,
        tenant_id: str,
        user_id: str,
        context_type: Optional[str],
        effective_category: str,
    ) -> None:
        """Publish the primary-write lifecycle event."""
        memory_events = self._memory_events
        if memory_events is None:
            return
        memory_events.publish_nowait(
            MemoryStoredEvent(
                uri=ctx.uri,
                record_id=str(record["id"]),
                tenant_id=tenant_id,
                user_id=user_id,
                project_id=str(record.get("project_id", "")),
                context_type=str(
                    context_type or ctx.context_type or ContextType.MEMORY
                ),
                category=effective_category,
                content=content,
                record=dict(record),
            )
        )

    def _ttl_for_store_record(
        self,
        *,
        context_type: Optional[str],
        effective_category: str,
        meta: Dict[str, Any],
    ) -> str:
        """Return the TTL string for short-lived store record kinds."""
        if context_type == ContextType.STAGING:
            return self._ttl_from_hours(self._config.immediate_event_ttl_hours)
        if (
            (context_type or ContextType.MEMORY) == ContextType.MEMORY
            and effective_category == "events"
            and meta.get("layer") == "merged"
        ):
            return self._ttl_from_hours(self._config.merged_event_ttl_hours)
        return ""

    @staticmethod
    def _populate_store_source_fields(
        record: Dict[str, Any],
        meta: Dict[str, Any],
    ) -> None:
        """Copy document/conversation enrichment fields to top level."""
        record["source_doc_id"] = meta.get("source_doc_id", "")
        record["source_doc_title"] = meta.get("source_doc_title", "")
        record["source_section_path"] = meta.get("source_section_path", "")
        record["chunk_role"] = meta.get("chunk_role", "")
        record["speaker"] = meta.get("speaker", "")
        record["event_date"] = meta.get("event_date")

    @property
    def _write_dedup_service(self) -> "MemoryWriteDedupService":
        """Lazy-built collaborator for semantic deduplication."""
        from opencortex.services.memory_write_dedup_service import (
            MemoryWriteDedupService,
        )

        cached = getattr(self, "_write_dedup_service_instance", None)
        if cached is None:
            cached = MemoryWriteDedupService(self)
            self._write_dedup_service_instance = cached
        return cached

    @property
    def _context_builder(self) -> "MemoryWriteContextBuilder":
        """Lazy-built builder for write context assembly."""
        from opencortex.services.memory_write_context_builder import (
            MemoryWriteContextBuilder,
        )

        cached = getattr(self, "_context_builder_instance", None)
        if cached is None:
            cached = MemoryWriteContextBuilder(self)
            self._context_builder_instance = cached
        return cached

    @property
    def _write_derive_service(self) -> "MemoryWriteDeriveService":
        """Lazy-built collaborator for write-path derive coordination."""
        from opencortex.services.memory_write_derive_service import (
            MemoryWriteDeriveService,
        )

        cached = getattr(self, "_write_derive_service_instance", None)
        if cached is None:
            cached = MemoryWriteDeriveService(self)
            self._write_derive_service_instance = cached
        return cached

    @property
    def _write_embed_service(self) -> "MemoryWriteEmbedService":
        """Lazy-built collaborator for write-path embedding."""
        from opencortex.services.memory_write_embed_service import (
            MemoryWriteEmbedService,
        )

        cached = getattr(self, "_write_embed_service_instance", None)
        if cached is None:
            cached = MemoryWriteEmbedService(self)
            self._write_embed_service_instance = cached
        return cached

    @property
    def _mutation_service(self) -> "MemoryMutationService":
        """Lazy-built collaborator for update/remove mutations."""
        from opencortex.services.memory_mutation_service import MemoryMutationService

        cached = getattr(self, "_mutation_service_instance", None)
        if cached is None:
            cached = MemoryMutationService(self)
            self._mutation_service_instance = cached
        return cached

    async def _check_duplicate(
        self,
        vector: List[float],
        memory_kind: str,
        merge_signature: str,
        threshold: float,
        tid: str,
        uid: str,
    ) -> Optional[Tuple[str, float]]:
        """Return duplicate ``(existing_uri, score)`` when one exists."""
        return await self._write_dedup_service.check_duplicate(
            vector=vector,
            memory_kind=memory_kind,
            merge_signature=merge_signature,
            threshold=threshold,
            tid=tid,
            uid=uid,
        )

    async def _merge_into(
        self, existing_uri: str, new_abstract: str, new_content: str
    ) -> None:
        """Merge new content into an existing record and reinforce it."""
        await self._write_dedup_service.merge_into(
            existing_uri=existing_uri,
            new_abstract=new_abstract,
            new_content=new_content,
        )

    async def feedback(self, uri: str, reward: float) -> None:
        """Apply scoring feedback for write-time merge reinforcement."""
        await self._deps.feedback(uri, reward)

    async def _ensure_parent_records(self, parent_uri: str) -> None:
        """Ensure all ancestor directory records exist in the vector store."""
        await self._directory_record_service.ensure_parent_records(parent_uri)

    @property
    def _directory_record_service(self) -> "MemoryDirectoryRecordService":
        """Lazy-built collaborator for parent directory records."""
        from opencortex.services.memory_directory_record_service import (
            MemoryDirectoryRecordService,
        )

        cached = getattr(self, "_directory_record_service_instance", None)
        if cached is None:
            cached = MemoryDirectoryRecordService(self)
            self._directory_record_service_instance = cached
        return cached

    @property
    def _document_write_service(self) -> "MemoryDocumentWriteService":
        """Lazy-built collaborator for document and batch writes."""
        from opencortex.services.memory_document_write_service import (
            MemoryDocumentWriteService,
        )

        cached = getattr(self, "_document_write_service_instance", None)
        if cached is None:
            cached = MemoryDocumentWriteService(self)
            self._document_write_service_instance = cached
        return cached

    async def _generate_abstract_overview(
        self,
        content: str,
        file_path: str,
    ) -> tuple[str, str]:
        """Delegate document abstract/overview generation."""
        return await self._document_write_service._generate_abstract_overview(
            content,
            file_path,
        )

    async def _add_document(
        self,
        content: str,
        abstract: str,
        overview: str,
        category: str,
        parent_uri: Optional[str],
        context_type: str,
        meta: Optional[Dict[str, Any]],
        session_id: Optional[str],
        source_path: str,
    ) -> Context:
        """Delegate document ingest to MemoryDocumentWriteService."""
        return await self._document_write_service._add_document(
            content=content,
            abstract=abstract,
            overview=overview,
            category=category,
            parent_uri=parent_uri,
            context_type=context_type,
            meta=meta,
            session_id=session_id,
            source_path=source_path,
        )

    async def batch_add(
        self,
        items: List[Dict[str, Any]],
        source_path: str = "",
        scan_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Delegate batch writes to MemoryDocumentWriteService."""
        return await self._document_write_service.batch_add(
            items=items,
            source_path=source_path,
            scan_meta=scan_meta,
        )
