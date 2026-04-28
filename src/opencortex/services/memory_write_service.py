# SPDX-License-Identifier: Apache-2.0
"""Memory write/mutation service for OpenCortex.

This module owns add/update/remove/document ingest/batch write behavior while
MemoryService keeps the compatibility facade plus search/list/scoring methods.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from opencortex.core.context import Context, Vectorize
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import get_effective_identity
from opencortex.memory import MemoryKind
from opencortex.utils.uri import CortexURI

if TYPE_CHECKING:
    from opencortex.services.memory_document_write_service import (
        MemoryDocumentWriteService,
    )
    from opencortex.services.memory_service import MemoryService
    from opencortex.services.memory_store_record_service import MemoryStoreRecordService
    from opencortex.services.memory_write_dedup_service import MemoryWriteDedupService

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
        # Local import: orchestrator-private string helpers. Lazy so
        # the cycle (orchestrator imports memory_service in __init__,
        # memory_service imports from orchestrator at call time)
        # resolves cleanly. Future cleanup: extract these helpers to
        # ``opencortex/utils/strings.py``; out of scope for plan 010.
        from opencortex.services.derivation_service import (
            _merge_unique_strings,
            _split_keyword_string,
        )

        orch = self._orch
        orch._ensure_init()

        # Find existing record
        records = await orch._storage.filter(
            orch._get_collection(),
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            logger.warning("[MemoryService] Context not found: %s", uri)
            return False

        record = records[0]
        record_id = record.get("id", "")

        update_data: Dict[str, Any] = {}
        next_meta = record.get("meta", {})
        if isinstance(next_meta, str):
            try:
                next_meta = json.loads(next_meta)
            except (json.JSONDecodeError, TypeError):
                next_meta = {}
        elif not isinstance(next_meta, dict):
            next_meta = {}

        if meta:
            next_meta.update(meta)
            update_data["meta"] = next_meta
        if abstract is not None:
            update_data["abstract"] = abstract

        next_abstract = abstract if abstract is not None else record.get("abstract", "")
        next_content = content if content is not None else record.get("content", "")
        next_overview = overview if overview is not None else record.get("overview", "")
        next_entities = _merge_unique_strings(
            record.get("entities") or [],
            next_meta.get("entities"),
        )
        next_keywords_list = _merge_unique_strings(
            next_meta.get("topics"),
            _split_keyword_string(record.get("keywords", "")),
        )
        derived_fact_points: Optional[List[str]] = None
        if next_content and (abstract is not None or content is not None):
            # When content changed, force full LLM re-derivation so fact_points
            # are regenerated (ADV-001). Passing non-empty user_overview would
            # hit _derive_layers' fast-path which returns empty fact_points.
            derive_user_overview = "" if content is not None else next_overview
            derive_result = await orch._derive_layers(
                user_abstract=next_abstract,
                content=next_content,
                user_overview=derive_user_overview,
            )
            next_entities = _merge_unique_strings(
                derive_result.get("entities", []),
                next_entities,
            )
            next_keywords_list = _merge_unique_strings(
                next_keywords_list,
                _split_keyword_string(derive_result.get("keywords", "")),
            )
            next_anchor_handles = _merge_unique_strings(
                next_meta.get("anchor_handles"),
                derive_result.get("anchor_handles", []),
            )
            if next_anchor_handles:
                next_meta["anchor_handles"] = next_anchor_handles
            # ADV-001 fix: capture fact_points so _sync_anchor_projection_records
            # regenerates fp records instead of wiping them.
            raw_fps = derive_result.get("fact_points", [])
            derived_fact_points = (
                [str(fp) for fp in raw_fps] if isinstance(raw_fps, list) else []
            )
        if next_keywords_list:
            next_meta["topics"] = _merge_unique_strings(
                next_meta.get("topics"),
                next_keywords_list,
            )
            update_data["keywords"] = ", ".join(next_keywords_list)
        if next_entities:
            update_data["entities"] = next_entities
        if update_data.get("meta") is not None or next_meta:
            update_data["meta"] = next_meta
        if orch._embedder and (abstract is not None or content is not None):
            loop = asyncio.get_event_loop()
            embed_input = next_abstract
            if next_keywords_list:
                embed_input = f"{embed_input} {', '.join(next_keywords_list)}".strip()
            result = await loop.run_in_executor(
                None,
                orch._embedder.embed,
                embed_input,
            )
            update_data["vector"] = result.dense_vector
            if result.sparse_vector:
                update_data["sparse_vector"] = result.sparse_vector
        abstract_json = orch._build_abstract_json(
            uri=uri,
            context_type=record.get("context_type", ""),
            category=record.get("category", ""),
            abstract=next_abstract,
            overview=next_overview,
            content=next_content,
            entities=next_entities,
            meta=next_meta,
            keywords=next_keywords_list,
            parent_uri=record.get("parent_uri", ""),
            session_id=record.get("session_id", ""),
        )
        # ADV-001 fix: inject fact_points symmetric to add(). If
        # _derive_layers ran, use its fact_points. Otherwise (fast
        # path), preserve existing fact_points from the stored
        # abstract_json so _sync_anchor_projection_records does not
        # wipe them.
        if derived_fact_points is not None:
            abstract_json["fact_points"] = derived_fact_points
        else:
            prior_abstract_json = record.get("abstract_json")
            if isinstance(prior_abstract_json, dict):
                prior_fps = prior_abstract_json.get("fact_points") or []
                if isinstance(prior_fps, list):
                    abstract_json["fact_points"] = [str(fp) for fp in prior_fps]
        update_data.update(
            orch._memory_object_payload(
                abstract_json,
                is_leaf=bool(record.get("is_leaf", False)),
            )
        )
        update_data["abstract_json"] = abstract_json

        if update_data:
            await orch._storage.update(orch._get_collection(), record_id, update_data)
            updated_record = dict(record)
            updated_record.update(update_data)
            await orch._sync_anchor_projection_records(
                source_record=updated_record,
                abstract_json=abstract_json,
            )

        # Update filesystem
        if abstract is not None or content is not None or overview is not None:
            await orch._fs.write_context(
                uri=uri,
                content=next_content,
                abstract=next_abstract,
                overview=next_overview,
                abstract_json=abstract_json,
            )

        # Sync entity index if content/abstract changed (skip for non-leaf nodes)
        if (
            getattr(orch, "_entity_index", None)
            and (abstract is not None or content is not None)
            and record.get("is_leaf") is not False
        ):
            try:
                text_for_entities = content or abstract or ""
                if text_for_entities and orch._llm_completion:
                    derive_result = await orch._derive_layers(
                        user_abstract=abstract or record.get("abstract", ""),
                        content=text_for_entities,
                    )
                    new_entities = derive_result.get("entities", [])
                else:
                    new_entities = []
                orch._entity_index.update(
                    orch._get_collection(),
                    str(record_id),
                    new_entities,
                )
                if new_entities:
                    await orch._storage.update(
                        orch._get_collection(),
                        record_id,
                        {"entities": new_entities},
                    )
            except Exception as exc:
                logger.warning(
                    "[MemoryService] Entity sync on update failed: %s",
                    exc,
                )

        logger.info("[MemoryService] Updated context: %s", uri)
        return True

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
        orch = self._orch
        orch._ensure_init()

        # Pre-delete: get affected record IDs for entity index sync
        affected_ids_for_entity: List[str] = []
        if getattr(orch, "_entity_index", None):
            try:
                collection = orch._get_collection()
                # Use prefix match to catch recursive descendants
                # (remove_by_uri uses MatchText which is prefix-like)
                affected = await orch._storage.filter(
                    collection,
                    {"op": "prefix", "field": "uri", "prefix": uri},
                    limit=10000,
                )
                affected_ids_for_entity = [str(r["id"]) for r in affected]
            except Exception:
                pass

        # Remove from vector DB
        count = await orch._storage.remove_by_uri(orch._get_collection(), uri)

        # Post-delete: sync entity index
        if getattr(orch, "_entity_index", None) and affected_ids_for_entity:
            orch._entity_index.remove_batch(
                orch._get_collection(),
                affected_ids_for_entity,
            )

        # Remove from filesystem
        try:
            await orch._fs.rm(uri, recursive=recursive)
        except Exception as exc:
            logger.warning(
                "[MemoryService] FS removal failed for %s: %s",
                uri,
                exc,
            )

        logger.info("[MemoryService] Removed %d records for: %s", count, uri)
        return count

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
        from opencortex.orchestrator import (
            _merge_unique_strings,
            _split_keyword_string,
        )

        orch = self._orch
        orch._ensure_init()
        meta = dict(meta or {})
        explicit_entities = _merge_unique_strings(meta.get("entities"))
        explicit_topics = _merge_unique_strings(meta.get("topics"))

        # Determine ingestion mode
        from opencortex.ingest.resolver import IngestModeResolver

        ingest_mode = IngestModeResolver.resolve(
            content=content,
            meta=meta,
            source_path=meta.get("source_path", ""),
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
                source_path=meta.get("source_path", ""),
            )

        add_started = asyncio.get_running_loop().time()
        derive_layers_ms = 0
        embed_ms = 0
        dedup_ms = 0
        upsert_ms = 0
        fs_write_ms = 0

        # Build URI if not provided
        if not uri:
            uri = orch._auto_uri(context_type or "memory", category, abstract=abstract)
            uri = await orch._resolve_unique_uri(uri)
            existing_record = None
        else:
            existing_record = await orch._get_record_by_uri(uri)

        # Build parent URI if not provided
        if not parent_uri:
            parent_uri = orch._derive_parent_uri(uri)

        # Derive L0/L1/keywords from L2 via structured LLM calls
        keywords = ""
        layers = {}
        if content and is_leaf and not defer_derive:
            derive_started = asyncio.get_running_loop().time()
            layers = await orch._derive_layers(
                user_abstract=abstract,
                content=content,
                user_overview=overview,
            )
            derive_layers_ms = int(
                (asyncio.get_running_loop().time() - derive_started) * 1000,
            )
            if not abstract:
                abstract = layers["abstract"]
            if not overview:
                overview = layers["overview"]
            keywords = layers["keywords"]
            entities = _merge_unique_strings(
                layers.get("entities", []),
                explicit_entities,
            )
        elif content and is_leaf and defer_derive:
            # Deferred derive: use deterministic truncation as placeholder
            if not overview:
                overview = orch._fallback_overview_from_content(
                    user_overview=overview,
                    content=content,
                )
            if not abstract:
                abstract = orch._derive_abstract_from_overview(
                    user_abstract=abstract,
                    overview=overview,
                    content=content,
                )
            entities = explicit_entities
        else:
            entities = explicit_entities

        keywords_list = _merge_unique_strings(
            _split_keyword_string(keywords),
            explicit_topics,
        )
        if keywords_list:
            meta["topics"] = _merge_unique_strings(meta.get("topics"), keywords_list)
        anchor_handles = _merge_unique_strings(
            meta.get("anchor_handles"),
            (layers.get("anchor_handles", []) if content and is_leaf else []),
        )
        if anchor_handles:
            meta["anchor_handles"] = anchor_handles
        keywords = ", ".join(keywords_list)

        # Build effective user identity (per-request or config default)
        tid, uid = get_effective_identity()
        effective_user = UserIdentifier(tid, uid)

        # Create context object
        ctx = Context(
            uri=uri,
            parent_uri=parent_uri,
            is_leaf=is_leaf,
            abstract=abstract,
            overview=overview,
            context_type=context_type,
            category=category,
            related_uri=related_uri or [],
            meta=meta,
            session_id=session_id,
            user=effective_user,
            id=(
                str(existing_record.get("id", "") or "")
                if existing_record is not None
                else None
            ),
        )

        # Override vectorization text.
        # Priority: embed_text > abstract+keywords > abstract (default from Context)
        base_text = embed_text or abstract
        if keywords:
            ctx.vectorize = Vectorize(f"{base_text} {keywords}")
        elif embed_text:
            ctx.vectorize = Vectorize(embed_text)

        effective_category = category or orch._extract_category_from_uri(uri)
        abstract_json = orch._build_abstract_json(
            uri=uri,
            context_type=context_type or "",
            category=effective_category,
            abstract=abstract,
            overview=overview,
            content=content,
            entities=entities,
            meta=meta,
            keywords=keywords_list,
            parent_uri=parent_uri,
            session_id=session_id,
        )
        # Inject fact_points from LLM derivation so _sync_anchor_projection_records
        # can generate fact_point records. Only present when content+is_leaf path ran.
        if content and is_leaf:
            abstract_json["fact_points"] = layers.get("fact_points", [])
        object_payload = orch._memory_object_payload(abstract_json, is_leaf=is_leaf)
        memory_kind = MemoryKind(object_payload["memory_kind"])
        merge_signature = str(object_payload["merge_signature"])
        mergeable = bool(object_payload["mergeable"])

        # Embed (offload sync embedder to thread so we don't block the loop)
        result = None
        if orch._embedder:
            loop = asyncio.get_event_loop()
            embed_started = asyncio.get_running_loop().time()
            result = await loop.run_in_executor(
                None, orch._embedder.embed, ctx.get_vectorization_text()
            )
            embed_ms = int((asyncio.get_running_loop().time() - embed_started) * 1000)
            ctx.vector = result.dense_vector

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

        sparse_vector = (
            result.sparse_vector
            if orch._embedder and result is not None and result.sparse_vector
            else None
        )
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
            sparse_vector=sparse_vector,
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
        orch = self._orch
        uri = parent_uri
        to_create = []

        # Walk up the URI tree, collecting missing directories
        while uri:
            try:
                parsed = CortexURI(uri)
            except ValueError:
                break

            # Check if this directory record already exists
            existing = await orch._storage.filter(
                orch._get_collection(),
                {"op": "must", "field": "uri", "conds": [uri]},
                limit=1,
            )
            if existing:
                break  # This level and all above already exist

            to_create.append(uri)

            parent = parsed.parent
            if parent is None:
                break
            uri = str(parent)

        # Build effective user identity (per-request or config default)
        tid, uid = get_effective_identity()
        effective_user = UserIdentifier(tid, uid)

        # Create directory records from top down (so parent_uri links are valid)
        for dir_uri in reversed(to_create):
            dir_parent = orch._derive_parent_uri(dir_uri)
            dir_ctx = Context(
                uri=dir_uri,
                parent_uri=dir_parent,
                is_leaf=False,
                abstract="",
                user=effective_user,
            )

            # Embed the directory name as a minimal vector
            dir_name = dir_uri.rstrip("/").rsplit("/", 1)[-1]
            embed_result = None
            if orch._embedder and dir_name:
                loop = asyncio.get_event_loop()
                embed_result = await loop.run_in_executor(
                    None, orch._embedder.embed, dir_name
                )
                dir_ctx.vector = embed_result.dense_vector

            record = dir_ctx.to_dict()
            if dir_ctx.vector:
                record["vector"] = dir_ctx.vector
            if embed_result and embed_result.sparse_vector:
                record["sparse_vector"] = embed_result.sparse_vector
            # Populate scope fields so directory records pass scope filters
            record["scope"] = "private" if CortexURI(dir_uri).is_private else "shared"
            record["source_user_id"] = uid
            record["source_tenant_id"] = tid
            record["category"] = ""
            record["mergeable"] = False
            record["session_id"] = ""
            record["ttl_expires_at"] = ""
            await orch._storage.upsert(orch._get_collection(), record)
            logger.debug("[MemoryService] Created directory record: %s", dir_uri)

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
