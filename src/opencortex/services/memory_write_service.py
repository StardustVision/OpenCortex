# SPDX-License-Identifier: Apache-2.0
"""Memory write/mutation service for OpenCortex.

This module owns add/update/remove/document ingest/batch write behavior while
MemoryService keeps the compatibility facade plus search/list/scoring methods.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from opencortex.core.context import Context
from opencortex.http.request_context import get_effective_identity
from opencortex.memory import MemoryKind

if TYPE_CHECKING:
    from opencortex.services.memory_directory_record_service import (
        MemoryDirectoryRecordService,
    )
    from opencortex.services.memory_document_write_service import (
        MemoryDocumentWriteService,
    )
    from opencortex.services.memory_mutation_service import MemoryMutationService
    from opencortex.services.memory_service import MemoryService
    from opencortex.services.memory_store_record_service import MemoryStoreRecordService
    from opencortex.services.memory_write_context_builder import (
        MemoryWriteContextBuilder,
    )
    from opencortex.services.memory_write_dedup_service import MemoryWriteDedupService
    from opencortex.services.memory_write_derive_service import MemoryWriteDeriveService
    from opencortex.services.memory_write_embed_service import MemoryWriteEmbedService

logger = logging.getLogger(__name__)


class MemoryWriteService:
    """Own memory write/mutation logic behind the MemoryService facade."""

    def __init__(self, memory_service: "MemoryService") -> None:
        self._service = memory_service

    @property
    def _orch(self) -> Any:
        return self._service._orch

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
            dedup: Enable semantic dedup check before write.
            dedup_threshold: Cosine similarity threshold for dedup.
            embed_text: Override text used for embedding (takes
                priority over abstract + keywords).
            defer_derive: Skip LLM derivation; use truncation as
                placeholder.

        Returns:
            The created ``Context`` with ``meta["dedup_action"]`` set
            to ``"created"`` or ``"merged"``.
        """
        orch = self._orch
        orch._ensure_init()

        # Determine ingestion mode
        from opencortex.ingest.resolver import IngestModeResolver

        ingest_mode = IngestModeResolver.resolve(
            content=content,
            meta=meta or {},
            source_path=(meta or {}).get("source_path", ""),
            session_id=session_id or "",
        )

        # Document mode: parse -> chunks -> write each with hierarchy
        if ingest_mode == "document" and content and is_leaf:
            return await self._service._add_document(
                content=content,
                abstract=abstract,
                overview=overview,
                category=category,
                parent_uri=parent_uri,
                context_type=context_type or "resource",
                meta=meta,
                session_id=session_id,
                source_path=(meta or {}).get("source_path", ""),
            )

        add_started = asyncio.get_running_loop().time()
        embed_ms = 0
        dedup_ms = 0
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
        parent_uri = target.parent_uri

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
        memory_kind = MemoryKind(object_payload["memory_kind"])
        merge_signature = assembled.merge_signature
        mergeable = assembled.mergeable

        embed_result = await self._write_embed_service.embed_for_write(ctx)
        embed_ms = embed_result.embed_ms

        # --- Write-time semantic dedup ---
        if dedup and ctx.vector and is_leaf and mergeable:
            dedup_result = await self._write_dedup_service.try_merge_duplicate(
                ctx=ctx,
                vector=ctx.vector,
                memory_kind=memory_kind.value,
                merge_signature=merge_signature,
                threshold=dedup_threshold,
                tenant_id=tid,
                user_id=uid,
                abstract=abstract,
                content=content,
                add_started=add_started,
            )
            dedup_ms = dedup_result.dedup_ms
            if dedup_result.merged and dedup_result.ctx is not None:
                logger.info(
                    "[MemoryService] add tenant=%s user=%s uri=%s "
                    "dedup_action=merged dedup_target=%s score=%.3f "
                    "timing_ms(total=%d derive_layers=%d embed=%d dedup=%d "
                    "upsert=%d fs_write=%d)",
                    tid,
                    uid,
                    uri,
                    dedup_result.target_uri,
                    dedup_result.score,
                    dedup_result.total_ms_at_match,
                    derive_layers_ms,
                    embed_ms,
                    dedup_ms,
                    upsert_ms,
                    fs_write_ms,
                )
                return dedup_result.ctx

        # Ensure parent directory records exist in vector DB
        if is_leaf and parent_uri:
            await self._service._ensure_parent_records(parent_uri)

        store_result = await self._store_record_service.persist_context_record(
            ctx=ctx,
            content=content,
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
            is_leaf=is_leaf,
        )
        upsert_ms = store_result.upsert_ms
        fs_write_ms = 0  # Non-blocking

        ctx.meta["dedup_action"] = "created"
        total_ms = int((asyncio.get_running_loop().time() - add_started) * 1000)
        logger.info(
            "[MemoryService] add tenant=%s user=%s uri=%s dedup_action=created "
            "timing_ms(total=%d derive_layers=%d embed=%d dedup=%d "
            "upsert=%d fs_write=%d)",
            tid,
            uid,
            uri,
            total_ms,
            derive_layers_ms,
            embed_ms,
            dedup_ms,
            upsert_ms,
            fs_write_ms,
        )
        return ctx

    # ------------------------------------------------------------------
    # Write-time dedup helpers
    # ------------------------------------------------------------------

    @property
    def _store_record_service(self) -> "MemoryStoreRecordService":
        """Lazy-built service for store record persistence."""
        from opencortex.services.memory_store_record_service import (
            MemoryStoreRecordService,
        )

        cached = getattr(self, "_store_record_service_instance", None)
        if cached is None:
            cached = MemoryStoreRecordService(self)
            self._store_record_service_instance = cached
        return cached

    @property
    def _write_dedup_service(self) -> "MemoryWriteDedupService":
        """Lazy-built service for write-time semantic deduplication."""
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
        """Lazy-built service for write-path derive coordination."""
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
        """Lazy-built service for write-path embedding."""
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
        """Lazy-built service for update/remove mutations."""
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

    async def _ensure_parent_records(self, parent_uri: str) -> None:
        """Ensure all ancestor directory records exist in the vector store."""
        await self._directory_record_service.ensure_parent_records(parent_uri)

    @property
    def _directory_record_service(self) -> "MemoryDirectoryRecordService":
        """Lazy-built service for parent directory records."""
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
        """Lazy-built service for document and batch writes."""
        from opencortex.services.memory_document_write_service import (
            MemoryDocumentWriteService,
        )

        cached = getattr(self, "_document_write_service_instance", None)
        if cached is None:
            cached = MemoryDocumentWriteService(self._service)
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
