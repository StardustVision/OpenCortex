# SPDX-License-Identifier: Apache-2.0
"""Memory record CRUD + scoring service extracted from MemoryOrchestrator.

All 14 public methods have been extracted from ``MemoryOrchestrator``
as part of plans 010/011. This module owns memory record lifecycle:
creation, retrieval, update, removal, and reward-based scoring.

Boundary
--------
``MemoryService`` is responsible for:
- Memory record CRUD: ``add``, ``update``, ``remove``, ``batch_add``
- Memory record queries: ``search``, ``list_memories``, ``memory_index``,
  ``list_memories_admin``
- Memory record scoring + lifecycle adjuncts: ``feedback``,
  ``feedback_batch``, ``decay``, ``cleanup_expired_staging``,
  ``protect``, ``get_profile``

It is explicitly NOT responsible for:
- Knowledge management (``knowledge_*``, archivist) — Phase 2
- System status reporting — Phase 4 (``SystemStatusService``)
- Subsystem boot sequencing — Phase 5
- Periodic background tasks (autophagy / connection sweepers / derive
  worker) — Phase 6
- Conversation lifecycle (``session_*``, benchmark ingest) — already
  delegated to ``ContextManager``
- Storage adapters, embedders, recall planning, intent routing — owned
  by their respective modules

Design
------
The service holds a back-reference to the orchestrator
(``self._orch``) and reaches into orchestrator-owned subsystems
(``_storage``, ``_embedder``, ``_fs``, ``_recall_planner``, etc.) at
call time. This mirrors the precedent set by
``BenchmarkConversationIngestService``. Phase 5's
``SubsystemBootstrapper`` will eventually replace the back-reference
with a typed ``SubsystemContainer`` parameter; doing both swaps in
one PR would be needless churn.

Construction is sync and cheap — no I/O, no model loading. The
orchestrator builds a single ``MemoryService`` instance in
``__init__`` so that delegate methods can blindly call
``self._memory_service.X`` without ``if None`` guards.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from uuid import uuid4

from opencortex.core.context import Context, Vectorize
from opencortex.core.user_id import UserIdentifier
from opencortex.cognition.state_types import OwnerType
from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.memory import MemoryKind
from opencortex.intent import (
    RetrievalDepth,
    RetrievalPlan,
    SearchResult,
)
from opencortex.intent.retrieval_support import (
    build_probe_scope_input,
    build_scope_filter,
)
from opencortex.intent.timing import (
    StageTimingCollector,
    measure_async,
    measure_sync,
)
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    FindResult,
    QueryResult,
    TypedQuery,
)
from opencortex.utils.uri import CortexURI

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator

logger = logging.getLogger(__name__)

# Maximum number of batch_add items processed concurrently
_BATCH_ADD_CONCURRENCY = 8


class MemoryService:
    """Memory record CRUD + scoring surface.

    All public methods (14 total) have been extracted from
    ``MemoryOrchestrator`` across plans 010/011. The service is
    constructed eagerly by the orchestrator and delegates to
    orchestrator-owned subsystems via ``self._orch``.
    """

    def __init__(self, orchestrator: "MemoryOrchestrator") -> None:
        """Bind the service to its parent orchestrator.

        Args:
            orchestrator: The ``MemoryOrchestrator`` instance whose
                subsystems (``_storage``, ``_embedder``, ``_fs``,
                ``_recall_planner``, etc.) this service reaches into
                at call time. Stored as ``self._orch``; not validated.
        """
        self._orch = orchestrator

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
        from opencortex.orchestrator import (
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
                None, orch._embedder.embed, embed_input,
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
                    orch._get_collection(), str(record_id), new_entities,
                )
                if new_entities:
                    await orch._storage.update(
                        orch._get_collection(), record_id, {"entities": new_entities},
                    )
            except Exception as exc:
                logger.warning(
                    "[MemoryService] Entity sync on update failed: %s", exc,
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
                orch._get_collection(), affected_ids_for_entity,
            )

        # Remove from filesystem
        try:
            await orch._fs.rm(uri, recursive=recursive)
        except Exception as exc:
            logger.warning(
                "[MemoryService] FS removal failed for %s: %s", uri, exc,
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
            return await self._add_document(
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
                    user_overview=overview, content=content,
                )
            if not abstract:
                abstract = orch._derive_abstract_from_overview(
                    user_abstract=abstract, overview=overview, content=content,
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
        if embed_text:
            base_text = embed_text
        else:
            base_text = abstract
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
            dedup_started = asyncio.get_running_loop().time()
            dup = await self._check_duplicate(
                vector=ctx.vector,
                memory_kind=memory_kind.value,
                merge_signature=merge_signature,
                threshold=dedup_threshold,
                tid=tid,
                uid=uid,
            )
            dedup_ms = int((asyncio.get_running_loop().time() - dedup_started) * 1000)
            if dup:
                existing_uri, existing_score = dup
                total_ms = int((asyncio.get_running_loop().time() - add_started) * 1000)
                existing_record = await orch._get_record_by_uri(existing_uri)
                persisted_owner_id = ""
                persisted_project_id = get_effective_project_id()
                if existing_record:
                    persisted_owner_id = str(existing_record.get("id", ""))
                    persisted_project_id = str(
                        existing_record.get("project_id", persisted_project_id)
                    )
                await self._merge_into(existing_uri, abstract, content)
                await orch._initialize_autophagy_owner_state(
                    owner_type=OwnerType.MEMORY,
                    owner_id=persisted_owner_id,
                    tenant_id=tid,
                    user_id=uid,
                    project_id=persisted_project_id,
                )
                logger.info(
                    "[MemoryService] add tenant=%s user=%s uri=%s "
                    "dedup_action=merged dedup_target=%s score=%.3f "
                    "timing_ms(total=%d derive_layers=%d embed=%d dedup=%d upsert=%d fs_write=%d)",
                    tid,
                    uid,
                    uri,
                    existing_uri,
                    existing_score,
                    total_ms,
                    derive_layers_ms,
                    embed_ms,
                    dedup_ms,
                    upsert_ms,
                    fs_write_ms,
                )
                ctx.uri = existing_uri
                ctx.meta["dedup_action"] = "merged"
                ctx.meta["dedup_score"] = round(existing_score, 4)
                return ctx

        # Ensure parent directory records exist in vector DB
        if is_leaf and parent_uri:
            await self._ensure_parent_records(parent_uri)

        # Store in vector DB
        record = ctx.to_dict()
        if ctx.vector:
            record["vector"] = ctx.vector
        if orch._embedder and result.sparse_vector:
            record["sparse_vector"] = result.sparse_vector

        # Populate scope/category/source fields for path-redesign
        inferred_scope = "private" if CortexURI(uri).is_private else "shared"
        record["scope"] = inferred_scope
        record["category"] = effective_category
        record["source_user_id"] = uid
        record["session_id"] = session_id or ""
        record["ttl_expires_at"] = ""
        record["project_id"] = get_effective_project_id()
        record["source_tenant_id"] = tid
        record["keywords"] = keywords
        record["entities"] = entities
        record.update(object_payload)
        record["abstract_json"] = abstract_json

        # v0.6: Flatten doc/conversation enrichment fields to top-level payload
        record["source_doc_id"] = (meta or {}).get("source_doc_id", "")
        record["source_doc_title"] = (meta or {}).get("source_doc_title", "")
        record["source_section_path"] = (meta or {}).get("source_section_path", "")
        record["chunk_role"] = (meta or {}).get("chunk_role", "")
        record["speaker"] = (meta or {}).get("speaker", "")
        record["event_date"] = (meta or {}).get("event_date")

        # Set TTL for short-lived record types
        if context_type == "staging":
            record["ttl_expires_at"] = orch._ttl_from_hours(
                orch._config.immediate_event_ttl_hours
            )
        elif (
            (context_type or "memory") == "memory"
            and effective_category == "events"
            and (meta or {}).get("layer") == "merged"
        ):
            record["ttl_expires_at"] = orch._ttl_from_hours(
                orch._config.merged_event_ttl_hours
            )

        upsert_started = asyncio.get_running_loop().time()
        await orch._storage.upsert(orch._get_collection(), record)
        upsert_ms = int((asyncio.get_running_loop().time() - upsert_started) * 1000)
        await orch._sync_anchor_projection_records(
            source_record=record,
            abstract_json=abstract_json,
        )

        if (context_type or ctx.context_type or "memory") == "memory":
            await orch._initialize_autophagy_owner_state(
                owner_type=OwnerType.MEMORY,
                owner_id=str(record["id"]),
                tenant_id=tid,
                user_id=uid,
                project_id=record["project_id"],
            )

        # Sync EntityIndex (if available)
        _entity_idx = getattr(orch, '_entity_index', None)
        if _entity_idx and entities:
            _entity_idx.add(orch._get_collection(), str(record["id"]), entities)

        # CortexFS write — fire-and-forget (Qdrant upsert is the synchronous path)
        def _on_fs_done(t: asyncio.Task) -> None:
            """Handle completion of a fire-and-forget CortexFS write task."""
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.warning(
                    "[MemoryService] CortexFS write failed for %s: %s", uri, exc
                )

        _fs_task = asyncio.create_task(
            orch._fs.write_context(
                uri=uri,
                content=content,
                abstract=abstract,
                abstract_json=abstract_json,
                overview=overview,
                is_leaf=is_leaf,
            )
        )
        _fs_task.add_done_callback(_on_fs_done)
        fs_write_ms = 0  # Non-blocking

        ctx.meta["dedup_action"] = "created"
        total_ms = int((asyncio.get_running_loop().time() - add_started) * 1000)
        logger.info(
            "[MemoryService] add tenant=%s user=%s uri=%s dedup_action=created "
            "timing_ms(total=%d derive_layers=%d embed=%d dedup=%d upsert=%d fs_write=%d)",
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

    async def _check_duplicate(
        self,
        vector: List[float],
        memory_kind: str,
        merge_signature: str,
        threshold: float,
        tid: str,
        uid: str,
    ) -> Optional[Tuple[str, float]]:
        """Return ``(existing_uri, score)`` if a semantically similar record exists, else ``None``."""
        orch = self._orch
        try:
            # Build scope-aware filter: same tenant, same category, leaf only
            conds: list = [
                {"op": "must", "field": "source_tenant_id", "conds": [tid]},
                {"op": "must", "field": "is_leaf", "conds": [True]},
            ]
            if memory_kind:
                conds.append(
                    {"op": "must", "field": "memory_kind", "conds": [memory_kind]}
                )
            if merge_signature:
                conds.append(
                    {
                        "op": "must",
                        "field": "merge_signature",
                        "conds": [merge_signature],
                    }
                )
            # Scope: shared OR (private AND own user)
            conds.append(
                {
                    "op": "or",
                    "conds": [
                        {"op": "must", "field": "scope", "conds": ["shared"]},
                        {
                            "op": "and",
                            "conds": [
                                {"op": "must", "field": "scope", "conds": ["private"]},
                                {
                                    "op": "must",
                                    "field": "source_user_id",
                                    "conds": [uid],
                                },
                            ],
                        },
                    ],
                }
            )
            # Project isolation: only dedup within same project
            project_id = get_effective_project_id()
            if project_id:
                conds.append(
                    {"op": "must", "field": "project_id", "conds": [project_id]}
                )

            dedup_filter = {"op": "and", "conds": conds}

            results = await orch._storage.search(
                orch._get_collection(),
                query_vector=vector,
                filter=dedup_filter,
                limit=1,
                output_fields=["uri", "abstract"],
            )
            if results:
                score = results[0].get("_score", results[0].get("score", 0.0))
                if score >= threshold:
                    return (results[0]["uri"], score)
        except Exception as exc:
            logger.debug("[MemoryService] Dedup check failed: %s", exc)
        return None

    async def _merge_into(
        self, existing_uri: str, new_abstract: str, new_content: str
    ) -> None:
        """Merge new content into an existing record and apply positive reinforcement."""
        orch = self._orch
        records = await orch._storage.filter(
            orch._get_collection(),
            {"op": "must", "field": "uri", "conds": [existing_uri]},
            limit=1,
            output_fields=["abstract", "overview"],
        )
        existing_content = ""
        if records:
            # Read existing L2 content from filesystem
            try:
                existing_content = await orch._fs.read_file(existing_uri)
            except Exception:
                existing_content = ""

        merged_content = (
            f"{existing_content}\n---\n{new_content}".strip()
            if new_content
            else existing_content
        )
        await self.update(existing_uri, abstract=new_abstract, content=merged_content)
        # Positive reinforcement for the merged record
        await self.feedback(existing_uri, 0.5)

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
        """Parse content into chunks via ParserRegistry and write each to CortexFS + Qdrant."""
        orch = self._orch
        if orch._parser_registry is None:
            from opencortex.parse.registry import ParserRegistry

            orch._parser_registry = ParserRegistry()
        registry = orch._parser_registry
        if source_path:
            parser = registry.get_parser_for_file(source_path)
        else:
            parser = None

        if parser:
            chunks = await parser.parse_content(content, source_path=source_path)
        else:
            chunks = await registry.parse_content(content, source_format="markdown")

        # --- v0.6: Generate source_doc_id for document scoped search ---
        _effective_source_path = (
            source_path
            or (meta or {}).get("source_path", "")
            or (meta or {}).get("file_path", "")
        )
        if _effective_source_path:
            source_doc_id = hashlib.sha256(_effective_source_path.encode()).hexdigest()[
                :16
            ]
        else:
            source_doc_id = uuid4().hex[:16]
        source_doc_title = (meta or {}).get("title", "")
        if not source_doc_title and _effective_source_path:
            source_doc_title = os.path.basename(_effective_source_path)

        # Single chunk or no chunks -> fall through to memory mode
        if len(chunks) <= 1:
            single_content = chunks[0].content if chunks else content
            embed_text = ""
            if orch._config.context_flattening_enabled:
                parts = []
                if source_doc_title:
                    parts.append(f"[{source_doc_title}]")
                sp = chunks[0].meta.get("section_path", "") if chunks else ""
                if sp:
                    parts.append(f"[{sp}]")
                parts.append(abstract)
                embed_text = " ".join(parts)
            return await self.add(
                abstract=abstract,
                content=single_content,
                category=category,
                parent_uri=parent_uri,
                context_type=context_type,
                meta={
                    **(meta or {}),
                    "ingest_mode": "memory",
                    "source_doc_id": source_doc_id,
                    "source_doc_title": source_doc_title,
                    "source_section_path": chunks[0].meta.get("section_path", "")
                    if chunks
                    else "",
                    "chunk_role": "document",
                },
                session_id=session_id,
                embed_text=embed_text,
            )

        # Multi-chunk: async derive -- return immediately, process in background
        doc_title = (
            Path(source_path).stem
            if source_path
            else abstract
            if abstract
            else "Document"
        )

        # Phase A: generate URI, write CortexFS, enqueue, return
        import json as _json

        parent_uri_candidate = orch._auto_uri(
            context_type or "resource", category, abstract=doc_title
        )
        parent_uri_candidate = await orch._resolve_unique_uri(parent_uri_candidate)
        while parent_uri_candidate in orch._inflight_derive_uris:
            parent_uri_candidate = await orch._resolve_unique_uri(
                parent_uri_candidate + "_"
            )
        orch._inflight_derive_uris.add(parent_uri_candidate)

        tid, uid = get_effective_identity()

        # Write .derive_pending marker first (recovery signal)
        marker_data = _json.dumps({
            "parent_uri": parent_uri_candidate,
            "category": category,
            "context_type": context_type or "resource",
            "source_path": source_path or "",
            "source_doc_id": source_doc_id,
            "source_doc_title": source_doc_title,
            "meta": meta or {},
            "tenant_id": tid,
            "user_id": uid,
        }).encode("utf-8")
        fs_path = orch._fs._uri_to_path(parent_uri_candidate)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: (
            orch._fs.agfs.mkdir(fs_path),
            orch._fs.agfs.write(f"{fs_path}/.derive_pending", marker_data),
        ))

        # Write L2 content to CortexFS
        await orch._fs.write_context(
            uri=parent_uri_candidate, content=content
        )

        # Enqueue derive task
        from opencortex.orchestrator import _DeriveTask

        task = _DeriveTask(
            parent_uri=parent_uri_candidate,
            content=content,
            abstract=doc_title,
            chunks=chunks,
            category=category,
            context_type=context_type or "resource",
            meta=meta or {},
            session_id=session_id,
            source_path=source_path or "",
            source_doc_id=source_doc_id,
            source_doc_title=source_doc_title,
            tenant_id=tid,
            user_id=uid,
        )
        await orch._derive_queue.put(task)

        logger.info(
            "[MemoryService] Document enqueued for async derive: %s (%d chunks)",
            parent_uri_candidate,
            len(chunks),
        )

        return Context(
            uri=parent_uri_candidate,
            abstract=doc_title,
            context_type=context_type or "resource",
            category=category,
            is_leaf=False,
            meta={**(meta or {}), "dedup_action": "created", "derive_pending": True},
            session_id=session_id,
        )

    # =========================================================================
    # Batch (U2 of plan 011)
    # =========================================================================

    async def batch_add(
        self,
        items: List[Dict[str, Any]],
        source_path: str = "",
        scan_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Batch-add documents with LLM-generated abstracts and overviews.

        When ``scan_meta`` is present, builds a directory hierarchy from
        ``meta.file_path`` values before writing leaf records.

        Args:
            items: List of dicts with ``content``, ``meta``,
                ``category``, etc.
            source_path: Source path hint for the batch.
            scan_meta: Scan metadata for directory tree building.

        Returns:
            Dict with keys ``status``, ``total``, ``imported``,
            ``errors``, ``uris``, ``has_git_project``, ``project_id``.
        """
        orch = self._orch
        orch._ensure_init()

        imported = 0
        errors: List[Dict[str, Any]] = []
        uris: List[str] = []

        # Hierarchical tree building when scan_meta present
        dir_uris: Dict[str, str] = {}
        if scan_meta:
            from pathlib import PurePosixPath

            # Collect unique directories
            all_dirs: set = set()
            for item in items:
                fp = (item.get("meta") or {}).get("file_path", "")
                if fp:
                    parts = PurePosixPath(fp).parts
                    for j in range(1, len(parts)):
                        all_dirs.add("/".join(parts[:j]))

            # Create directory nodes bottom-up (sorted by depth)
            for d in sorted(all_dirs, key=lambda x: x.count("/")):
                parent_dir = str(PurePosixPath(d).parent)
                parent_uri = dir_uris.get(parent_dir) if parent_dir != "." else None
                try:
                    dir_ctx = await orch.add(
                        abstract=PurePosixPath(d).name,
                        content="",
                        category="documents",
                        parent_uri=parent_uri,
                        is_leaf=False,
                        context_type="resource",
                        meta={
                            "source": "batch:scan",
                            "dir_path": d,
                            "ingest_mode": "memory",
                        },
                        dedup=False,
                    )
                    dir_uris[d] = dir_ctx.uri
                    uris.append(dir_ctx.uri)
                except Exception as exc:
                    logger.warning("[batch_add] Dir node failed for %s: %s", d, exc)

        sem = asyncio.Semaphore(_BATCH_ADD_CONCURRENCY)

        async def _process_one(i: int, item: dict) -> dict:
            """Process a single batch item: derive metadata and persist via add.

            Args:
                i: Zero-based index of the item within the batch.
                item: Raw item dict with content, meta, category, etc.

            Returns:
                A dict with ``uri`` and ``index`` on success, or ``error`` and
                ``index`` on failure.
            """
            async with sem:
                content = item.get("content", "")
                file_path = (item.get("meta") or {}).get("file_path", f"item_{i}")
                abstract, overview = await orch._generate_abstract_overview(
                    content, file_path
                )

                item_meta = dict(item.get("meta") or {})
                item_meta.setdefault("source", "batch:scan")
                item_meta["ingest_mode"] = "memory"

                parent_uri = None
                if scan_meta and file_path:
                    from pathlib import PurePosixPath

                    parent_dir = str(PurePosixPath(file_path).parent)
                    parent_uri = dir_uris.get(parent_dir)

                embed_text = ""
                if orch._config.context_flattening_enabled:
                    fp = item_meta.get("file_path", "")
                    if fp:
                        embed_text = f"[{fp}] {abstract}"

                try:
                    result = await orch.add(
                        abstract=abstract,
                        content=content,
                        overview=overview,
                        category=item.get("category", "documents"),
                        parent_uri=parent_uri,
                        context_type=item.get("context_type", "resource"),
                        meta=item_meta,
                        dedup=False,
                        embed_text=embed_text,
                    )
                    return {"uri": result.uri, "index": i}
                except Exception as exc:
                    return {"error": str(exc), "index": i}

        outcomes = await asyncio.gather(
            *[_process_one(i, item) for i, item in enumerate(items)],
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                errors.append({"error": str(outcome)})
            elif isinstance(outcome, dict) and "error" in outcome:
                errors.append({"index": outcome["index"], "error": outcome["error"]})
            else:
                uris.append(outcome["uri"])
                imported += 1

        has_git = (scan_meta or {}).get("has_git", False)
        project_id = (scan_meta or {}).get("project_id", "public")

        return {
            "status": "ok" if not errors else "partial",
            "total": len(items),
            "imported": imported,
            "errors": errors,
            "has_git_project": has_git and project_id != "public",
            "project_id": project_id,
            "uris": uris,
        }

    # =========================================================================
    # Scoring + lifecycle (U4 of plan 011)
    # =========================================================================

    async def feedback(self, uri: str, reward: float) -> None:
        """Submit a reward signal for a context.

        Positive rewards reinforce retrieval; negative rewards penalize
        it. The reinforced score formula:
        ``reinforced_score = similarity * (1 + alpha * reward_factor) * decay_factor``

        Args:
            uri: URI of the context.
            reward: Scalar reward value (positive = good, negative = bad).
        """
        orch = self._orch
        orch._ensure_init()

        # Find the record ID for this URI in context collection
        records = await orch._storage.filter(
            orch._get_collection(),
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            logger.warning("[MemoryService] feedback: URI not found: %s", uri)
            return

        record_id = records[0].get("id", "")
        if not record_id:
            return

        # Send reward via storage adapter
        if hasattr(orch._storage, "update_reward"):
            await orch._storage.update_reward(orch._get_collection(), record_id, reward)
            logger.info(
                "[MemoryService] Feedback sent: uri=%s, reward=%s",
                uri,
                reward,
            )
        else:
            logger.debug(
                "[MemoryService] Storage backend does not support rewards"
            )

        # Also update activity count
        ctx_data = records[0]
        active_count = ctx_data.get("active_count", 0)
        await orch._storage.update(
            orch._get_collection(),
            record_id,
            {"active_count": active_count + 1},
        )

    async def feedback_batch(self, rewards: List[Dict[str, Any]]) -> None:
        """Submit batch reward signals.

        Args:
            rewards: List of ``{"uri": str, "reward": float}`` dicts.
        """
        orch = self._orch
        orch._ensure_init()

        for item in rewards:
            await self.feedback(item["uri"], item["reward"])

    async def decay(self) -> Optional[Dict[str, Any]]:
        """Trigger time-decay across all records.

        Normal nodes decay at rate 0.95, protected nodes at rate 0.99.
        Records below threshold (0.01) may be archived.

        Returns:
            Decay summary dict with keys ``records_processed``,
            ``records_decayed``, ``records_below_threshold``,
            ``records_archived``, and optionally ``staging_cleaned``.
            ``None`` if the storage backend does not support decay.
        """
        orch = self._orch
        orch._ensure_init()

        if hasattr(orch._storage, "apply_decay"):
            result = await orch._storage.apply_decay()
            logger.info("[MemoryService] Decay applied: %s", result)
            decay_result = {
                "records_processed": result.records_processed,
                "records_decayed": result.records_decayed,
                "records_below_threshold": result.records_below_threshold,
                "records_archived": result.records_archived,
            }

            # Piggyback staging cleanup on decay
            try:
                cleaned = await self.cleanup_expired_staging()
                if cleaned:
                    decay_result["staging_cleaned"] = cleaned
            except Exception as exc:
                logger.warning("[MemoryService] Staging cleanup failed: %s", exc)

            return decay_result
        logger.debug("[MemoryService] Storage backend does not support decay")
        return None

    async def cleanup_expired_staging(self) -> int:
        """Delete records whose TTL has expired.

        Covers staging records, immediate-layer conversation records,
        and any other record with a non-empty ``ttl_expires_at`` field.

        Returns:
            Number of records deleted.
        """
        orch = self._orch
        orch._ensure_init()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Scan all records with non-empty ttl_expires_at
        expired = await orch._storage.filter(
            orch._get_collection(),
            {"op": "must_not", "field": "ttl_expires_at", "conds": [""]},
            limit=1000,
        )
        cleaned = 0
        to_delete = []
        for record in expired:
            ttl = record.get("ttl_expires_at", "")
            if ttl and ttl < now:
                rid = record.get("id", "")
                if rid:
                    to_delete.append(rid)
                uri = record.get("uri", "")
                if uri:
                    try:
                        await orch._fs.delete_temp(uri)
                    except Exception:
                        pass
                cleaned += 1
        if to_delete:
            await orch._storage.delete(orch._get_collection(), to_delete)
        if cleaned:
            logger.info("[MemoryService] Cleaned %d expired records", cleaned)
        return cleaned

    async def protect(self, uri: str, protected: bool = True) -> None:
        """Mark a context as protected to slow its decay rate.

        Protected memories decay at rate 0.99 instead of 0.95,
        preserving important knowledge for longer.

        Args:
            uri: URI of the context.
            protected: ``True`` to protect, ``False`` to unprotect.
        """
        orch = self._orch
        orch._ensure_init()

        records = await orch._storage.filter(
            orch._get_collection(),
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            logger.warning("[MemoryService] protect: URI not found: %s", uri)
            return

        record_id = records[0].get("id", "")
        if hasattr(orch._storage, "set_protected"):
            await orch._storage.set_protected(
                orch._get_collection(), record_id, protected
            )
            logger.info("[MemoryService] Set protected=%s for: %s", protected, uri)

    async def get_profile(self, uri: str) -> Optional[Dict[str, Any]]:
        """Get the feedback scoring profile for a context.

        Args:
            uri: URI of the context.

        Returns:
            Profile dict with keys ``reward_score``, ``retrieval_count``,
            ``positive_feedback_count``, ``negative_feedback_count``,
            ``effective_score``, ``is_protected``. ``None`` if the URI
            is not found or the backend does not support profiles.
        """
        orch = self._orch
        orch._ensure_init()

        records = await orch._storage.filter(
            orch._get_collection(),
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        if not records:
            return None

        record_id = records[0].get("id", "")
        if hasattr(orch._storage, "get_profile"):
            profile = await orch._storage.get_profile(orch._get_collection(), record_id)
            if profile:
                return {
                    "id": profile.id,
                    "reward_score": profile.reward_score,
                    "retrieval_count": profile.retrieval_count,
                    "positive_feedback_count": profile.positive_feedback_count,
                    "negative_feedback_count": profile.negative_feedback_count,
                    "effective_score": profile.effective_score,
                    "is_protected": profile.is_protected,
                }
        return None

    # =========================================================================
    # Queries (U3 of plan 011)
    # =========================================================================

    async def search(
        self,
        query: str,
        context_type: Optional[ContextType] = None,
        target_uri: str = "",
        limit: int = 5,
        score_threshold: Optional[float] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        detail_level: str = "l1",
        probe_result: Optional[SearchResult] = None,
        retrieve_plan: Optional[RetrievalPlan] = None,
        meta: Optional[Dict[str, Any]] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> FindResult:
        """Search for relevant contexts using probe-planner-runtime pipeline.

        Args:
            query: Natural language query string.
            context_type: Restrict to a specific type
                (memory/resource/skill).
            target_uri: Restrict search to a directory subtree.
            limit: Maximum results per type.
            score_threshold: Minimum relevance score.
            metadata_filter: Additional filter conditions.
            detail_level: Fallback detail level if planner does not
                override (``"l0"``, ``"l1"``, ``"l2"``).
            probe_result: Pre-computed probe result; computed when
                ``None``.
            retrieve_plan: Pre-computed retrieval plan; computed when
                ``None``.
            meta: Optional metadata dict (supports ``target_doc_id``).
            session_context: Optional session context for runtime scope.

        Returns:
            ``FindResult`` with ``memories``, ``resources``, and
            ``skills`` lists.
        """
        orch = self._orch
        orch._ensure_init()
        search_started = asyncio.get_running_loop().time()
        tid, uid = get_effective_identity()
        stage_timings = StageTimingCollector()

        target_doc_id = None
        if isinstance(meta, dict):
            target_doc_id = meta.get("target_doc_id")

        detail_level_value = (
            detail_level.value if isinstance(detail_level, DetailLevel) else detail_level
        )
        detail_level_override = (
            detail_level_value if detail_level_value != DetailLevel.L1.value else None
        )
        scope_filter = build_scope_filter(
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
            metadata_filter=metadata_filter,
        )

        if probe_result is None:
            probe_result = await measure_async(
                stage_timings,
                "probe",
                orch.probe_memory,
                query,
                context_type=context_type,
                target_uri=target_uri,
                target_doc_id=target_doc_id,
                session_context=session_context,
                metadata_filter=metadata_filter,
            )
        else:
            stage_timings.record_ms("probe", 0)
        if retrieve_plan is None:
            scope_input = build_probe_scope_input(
                context_type=context_type,
                target_uri=target_uri,
                target_doc_id=target_doc_id,
                session_context=session_context,
            )
            retrieve_plan = measure_sync(
                stage_timings,
                "plan",
                orch.plan_memory,
                query=query,
                probe_result=probe_result,
                max_items=limit,
                recall_mode="auto",
                detail_level_override=detail_level_override,
                scope_input=scope_input,
            )
        else:
            stage_timings.record_ms("plan", 0)
        intent_ms = (
            stage_timings.snapshot()["probe"] + stage_timings.snapshot()["plan"]
        )

        if retrieve_plan is None:
            total_ms = int((asyncio.get_running_loop().time() - search_started) * 1000)
            logger.debug(
                "[search] should_recall=False tenant=%s user=%s total_ms=%d",
                tid,
                uid,
                total_ms,
            )
            return FindResult(
                memories=[],
                resources=[],
                skills=[],
                probe_result=probe_result,
            )

        runtime_bound_plan = measure_sync(
            stage_timings,
            "bind",
            orch.bind_memory_runtime,
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
            max_items=limit,
            session_context=session_context,
            include_knowledge=False,
        )
        effective_limit = runtime_bound_plan["memory_limit"]
        detail_level = runtime_bound_plan["effective_depth"]
        typed_queries = self._build_typed_queries(
            query=query,
            context_type=context_type,
            target_uri=target_uri,
            retrieve_plan=retrieve_plan,
            runtime_bound_plan=runtime_bound_plan,
        )
        if target_doc_id:
            for typed_query in typed_queries:
                typed_query.target_doc_id = target_doc_id

        # Set target directories on queries if specified
        if target_uri:
            for tq in typed_queries:
                if not tq.target_directories:
                    tq.target_directories = [target_uri]

        search_filter = orch._build_search_filter(
            metadata_filter=scope_filter,
        )

        # Build retrieval coroutines
        retrieval_coros = [
            orch._execute_object_query(
                typed_query=tq,
                limit=effective_limit,
                score_threshold=score_threshold,
                search_filter=search_filter,
                retrieve_plan=retrieve_plan,
                probe_result=probe_result,
                bound_plan=runtime_bound_plan,
            )
            for tq in typed_queries
        ]

        query_results = await measure_async(
            stage_timings,
            "retrieve",
            asyncio.gather,
            *retrieval_coros,
        )
        query_results = list(query_results)
        hydration_actions: List[Dict[str, Any]] = []

        aggregate_started = asyncio.get_running_loop().time()
        result = orch._aggregate_results(query_results, limit=limit)
        result.probe_result = probe_result
        result.retrieve_plan = retrieve_plan
        retrieve_breakdown_ms = MemoryService._summarize_retrieve_breakdown(query_results)

        # Filter out directory nodes (is_leaf=False) — they exist for
        # hierarchical traversal but have no abstract/content of their own.
        result.memories = [m for m in result.memories if m.is_leaf]
        result.resources = [m for m in result.resources if m.is_leaf]
        result.skills = [m for m in result.skills if m.is_leaf]

        # Fire-and-forget: resolve URIs → record IDs → update access stats
        all_matched = result.memories + result.resources + result.skills

        stage_timings.record_elapsed("aggregate", aggregate_started)
        total_ms = int((asyncio.get_running_loop().time() - search_started) * 1000)
        stage_timings.record_ms("total", total_ms)
        timing_snapshot = stage_timings.snapshot()
        retrieval_latency_ms = (
            max(timing_snapshot["retrieve"], 0)
            + max(timing_snapshot.get("hydrate", 0), 0)
        )
        overhead_ms = timing_snapshot["overhead"]
        if runtime_bound_plan is not None:
            runtime_items = [
                {
                    "uri": mc.uri,
                    "context_type": mc.context_type.value,
                    "score": mc.score,
                }
                for mc in all_matched
            ]
            result.runtime_result = orch._memory_runtime.finalize(
                bound_plan=runtime_bound_plan,
                items=runtime_items,
                latency_ms=retrieval_latency_ms,
                stage_timing_ms=timing_snapshot,
                retrieve_breakdown_ms=retrieve_breakdown_ms,
                hydration_actions=hydration_actions,
            )
        logger.info(
            "[search] tenant=%s user=%s probe_candidates=%d queries=%d results=%d "
            "timing_ms(total=%d intent=%d retrieval=%d overhead=%d)",
            tid,
            uid,
            probe_result.evidence.candidate_count,
            len(typed_queries),
            len(all_matched),
            total_ms,
            intent_ms,
            retrieval_latency_ms,
            overhead_ms,
        )

        # v0.6: Build SearchExplainSummary
        if getattr(orch._config, "explain_enabled", True) and query_results:
            from opencortex.retrieve.types import SearchExplainSummary

            primary = query_results[0]
            result.explain_summary = SearchExplainSummary(
                total_ms=float(total_ms),
                query_count=len(query_results),
                primary_query_class=primary.explain.query_class
                if primary.explain
                else "",
                primary_path=primary.explain.path if primary.explain else "",
                doc_scope_hit=any(
                    qr.explain and qr.explain.doc_scope_hit for qr in query_results
                ),
                time_filter_hit=any(
                    qr.explain and qr.explain.time_filter_hit for qr in query_results
                ),
                rerank_triggered=any(
                    qr.explain and qr.explain.rerank_ms > 0 for qr in query_results
                ),
            )

        # Skill Engine: search active skills and merge into FindResult.skills
        if orch._skill_manager:
            try:
                from opencortex.retrieve.types import MatchedContext
                skill_results = await orch._skill_manager.search(
                    query, tid, uid, top_k=3,
                )
                for sr in skill_results:
                    result.skills.append(MatchedContext(
                        uri=sr.uri,
                        context_type=ContextType.SKILL,
                        is_leaf=True,
                        abstract=sr.abstract,
                        overview=sr.overview,
                        content=sr.content,
                        category=sr.category.value,
                        score=0.0,
                        session_id="",
                    ))
            except Exception as exc:
                logger.debug("[search] Skill search failed: %s", exc)

        result.total = len(result.memories) + len(result.resources) + len(result.skills)
        return result

    def _build_typed_queries(
        self,
        *,
        query: str,
        context_type: Optional[ContextType],
        target_uri: str,
        retrieve_plan: RetrievalPlan,
        runtime_bound_plan: Dict[str, Any],
    ) -> List[TypedQuery]:
        """Project planner posture into concrete ``TypedQuery`` list for retrieval."""
        if context_type:
            types_to_search = [context_type]
        elif target_uri:
            types_to_search = [MemoryService._infer_context_type(target_uri)]
        else:
            raw_context_types = runtime_bound_plan.get("context_types") or ["memory"]
            if len(raw_context_types) > 1:
                types_to_search = [ContextType.ANY]
            else:
                types_to_search = [
                    MemoryService._context_type_from_value(raw_value)
                    for raw_value in raw_context_types
                ]

        return [
            TypedQuery(
                query=query,
                context_type=ct,
                intent="memory",
                priority=1,
                target_directories=[target_uri] if target_uri else [],
                detail_level=MemoryService._detail_level_from_retrieval_depth(
                    retrieve_plan.retrieval_depth
                ),
            )
            for ct in types_to_search
        ]

    @staticmethod
    def _context_type_from_value(raw_value: str) -> ContextType:
        """Convert a raw string to a ContextType, defaulting to ANY on mismatch."""
        try:
            return ContextType(raw_value)
        except ValueError:
            return ContextType.ANY

    @staticmethod
    def _detail_level_from_retrieval_depth(
        retrieval_depth: RetrievalDepth,
    ) -> DetailLevel:
        """Map a RetrievalDepth enum value to its corresponding DetailLevel."""
        return DetailLevel(retrieval_depth.value)

    @staticmethod
    def _summarize_retrieve_breakdown(
        query_results: List[QueryResult],
    ) -> Dict[str, float]:
        """Aggregate per-query retrieval timings into a request-level breakdown."""
        keys = ("embed", "search", "rerank", "assemble", "total")
        if not query_results:
            return {key: 0.0 for key in keys}

        summary: Dict[str, float] = {}
        for key in keys:
            values = [
                float((query_result.timing_ms or {}).get(key, 0.0))
                for query_result in query_results
            ]
            summary[key] = round(max(values, default=0.0), 4)
        return summary

    @staticmethod
    def _infer_context_type(uri: str) -> ContextType:
        """Infer ContextType from URI path segments."""
        if "/staging/" in uri:
            return ContextType.STAGING
        elif "/memories/" in uri:
            return ContextType.MEMORY
        elif "/shared/cases/" in uri:
            return ContextType.CASE
        elif "/shared/patterns/" in uri:
            return ContextType.PATTERN
        elif "/skills/" in uri:
            return ContextType.SKILL
        return ContextType.RESOURCE

    async def list_memories(
        self,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        include_payload: bool = False,
    ) -> List[Dict[str, Any]]:
        """List user-accessible memories ordered by ``updated_at`` desc.

        Returns private (own) and shared memories, excluding staging
        records. Results are tenant-scoped and project-isolated.

        Args:
            category: Filter by category.
            context_type: Filter by context type.
            limit: Maximum records to return.
            offset: Pagination offset.
            include_payload: Include ``meta``, ``abstract_json``,
                ``overview``, and other enrichment fields.

        Returns:
            List of dicts with ``uri``, ``abstract``, ``category``,
            ``context_type``, ``scope``, ``project_id``, and timestamps.
        """
        orch = self._orch
        orch._ensure_init()
        tid, uid = get_effective_identity()

        # Same scope filter as search(): private own + shared
        scope_filter = {
            "op": "or",
            "conds": [
                {"op": "must", "field": "scope", "conds": ["shared", ""]},
                {
                    "op": "and",
                    "conds": [
                        {"op": "must", "field": "scope", "conds": ["private"]},
                        {"op": "must", "field": "source_user_id", "conds": [uid]},
                    ],
                },
            ],
        }

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
            scope_filter,
        ]
        if tid:
            conds.append(
                {"op": "must", "field": "source_tenant_id", "conds": [tid, ""]}
            )
        if category:
            conds.append({"op": "must", "field": "category", "conds": [category]})
        if context_type:
            conds.append(
                {"op": "must", "field": "context_type", "conds": [context_type]}
            )

        # Project filter: strict isolation
        project_id = get_effective_project_id()
        if project_id and project_id != "public":
            conds.append(
                {
                    "op": "or",
                    "conds": [
                        {
                            "op": "must",
                            "field": "project_id",
                            "conds": [project_id, "public"],
                        },
                    ],
                }
            )

        combined: Dict[str, Any] = {"op": "and", "conds": conds}

        records = await orch._storage.filter(
            orch._get_collection(),
            combined,
            limit=limit,
            offset=offset,
            order_by="updated_at",
            order_desc=True,
        )

        items: List[Dict[str, Any]] = []
        for record in records:
            if not record.get("abstract"):
                continue
            item = {
                "uri": record.get("uri", ""),
                "abstract": record.get("abstract", ""),
                "category": record.get("category", ""),
                "context_type": record.get("context_type", ""),
                "scope": record.get("scope", ""),
                "project_id": record.get("project_id", ""),
                "updated_at": record.get("updated_at", ""),
                "created_at": record.get("created_at", ""),
            }
            if include_payload:
                meta = dict(record.get("meta") or {})
                item.update(
                    {
                        "meta": record.get("meta", {}),
                        "abstract_json": record.get("abstract_json", {}),
                        "session_id": record.get("session_id", ""),
                        "speaker": record.get("speaker", ""),
                        "event_date": record.get("event_date", ""),
                        "overview": record.get("overview", ""),
                        "msg_range": meta.get("msg_range"),
                        "recomposition_stage": meta.get("recomposition_stage"),
                        "source_uri": meta.get("source_uri"),
                    }
                )
            items.append(item)
        return items

    async def memory_index(
        self,
        context_type: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Return a lightweight index of all memories grouped by context type.

        Only leaf records with non-empty abstracts are included.

        Args:
            context_type: Comma-separated list of context types to
                include. All types when ``None``.
            limit: Maximum records to scan.

        Returns:
            Dict with ``"index"`` mapping context type to a list of
            ``{uri, abstract, context_type, category, created_at}`` and
            ``"total"`` count.
        """
        orch = self._orch
        orch._ensure_init()
        tid, uid = get_effective_identity()

        scope_filter = {
            "op": "or",
            "conds": [
                {"op": "must", "field": "scope", "conds": ["shared", ""]},
                {
                    "op": "and",
                    "conds": [
                        {"op": "must", "field": "scope", "conds": ["private"]},
                        {"op": "must", "field": "source_user_id", "conds": [uid]},
                    ],
                },
            ],
        }

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
            {"op": "must", "field": "is_leaf", "conds": [True]},
            scope_filter,
        ]
        if tid:
            conds.append(
                {"op": "must", "field": "source_tenant_id", "conds": [tid, ""]}
            )

        if context_type:
            types = [t.strip() for t in context_type.split(",") if t.strip()]
            conds.append({"op": "must", "field": "context_type", "conds": types})

        project_id = get_effective_project_id()
        if project_id and project_id != "public":
            conds.append(
                {
                    "op": "or",
                    "conds": [
                        {
                            "op": "must",
                            "field": "project_id",
                            "conds": [project_id, "public"],
                        },
                    ],
                }
            )

        records = await orch._storage.filter(
            orch._get_collection(),
            {"op": "and", "conds": conds},
            limit=limit,
            offset=0,
            order_by="created_at",
            order_desc=True,
        )

        index: Dict[str, list] = {}
        for r in records:
            abstract = r.get("abstract", "")
            if not abstract:
                continue
            ct = r.get("context_type", "memory")
            if ct not in index:
                index[ct] = []
            index[ct].append(
                {
                    "uri": r.get("uri", ""),
                    "abstract": abstract[:150],
                    "context_type": ct,
                    "category": r.get("category", ""),
                    "created_at": r.get("created_at", ""),
                }
            )

        total = sum(len(v) for v in index.values())
        return {"index": index, "total": total}

    async def list_memories_admin(
        self,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        category: Optional[str] = None,
        context_type: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List memories across all users (admin-only, no scope isolation).

        Args:
            tenant_id: Filter by tenant.
            user_id: Filter by user.
            category: Filter by category.
            context_type: Filter by context type.
            limit: Maximum records to return.
            offset: Pagination offset.

        Returns:
            List of dicts with ``uri``, ``abstract``, ``category``,
            ``context_type``, ``scope``, ``project_id``, identity
            fields, and timestamps.
        """
        orch = self._orch
        orch._ensure_init()

        conds: List[Dict[str, Any]] = [
            {"op": "must_not", "field": "context_type", "conds": ["staging"]},
        ]
        if tenant_id:
            conds.append(
                {"op": "must", "field": "source_tenant_id", "conds": [tenant_id]}
            )
        if user_id:
            conds.append({"op": "must", "field": "source_user_id", "conds": [user_id]})
        if category:
            conds.append({"op": "must", "field": "category", "conds": [category]})
        if context_type:
            conds.append(
                {"op": "must", "field": "context_type", "conds": [context_type]}
            )

        combined: Dict[str, Any] = {"op": "and", "conds": conds}

        records = await orch._storage.filter(
            orch._get_collection(),
            combined,
            limit=limit,
            offset=offset,
            order_by="updated_at",
            order_desc=True,
        )

        return [
            {
                "uri": r.get("uri", ""),
                "abstract": r.get("abstract", ""),
                "category": r.get("category", ""),
                "context_type": r.get("context_type", ""),
                "scope": r.get("scope", ""),
                "project_id": r.get("project_id", ""),
                "source_tenant_id": r.get("source_tenant_id", ""),
                "source_user_id": r.get("source_user_id", ""),
                "updated_at": r.get("updated_at", ""),
                "created_at": r.get("created_at", ""),
            }
            for r in records
            if r.get("abstract")  # skip directory nodes (empty abstract)
        ]
