# SPDX-License-Identifier: Apache-2.0
"""Memory record CRUD + scoring service extracted from MemoryOrchestrator.

This module is Phase 1 of the
``docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md``
decomposition. It hosts the methods that read and write individual
memory records and adjust their reward / decay / protection state.

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
- System status reporting — Phase 3
- Subsystem boot sequencing — Phase 4
- Periodic background tasks (autophagy / connection sweepers / derive
  worker) — Phase 5
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
``BenchmarkConversationIngestService``. Phase 4's
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
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator

logger = logging.getLogger(__name__)


class MemoryService:
    """Memory record CRUD + scoring surface.

    Methods are added in subsequent units of plan 010 (U2 CRUD, U3
    queries, U4 scoring). U1 lands the scaffolding only so the move
    operations stay reviewable as one-method-per-commit diffs.
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
        affected_ids_for_entity: list[str] = []
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
