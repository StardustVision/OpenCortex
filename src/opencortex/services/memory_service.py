# SPDX-License-Identifier: Apache-2.0
"""Memory record facade plus search/listing service.

``MemoryService`` preserves the public method surface that
``MemoryOrchestrator`` delegates to, while write/mutation and scoring
domain logic live in narrower services.

Boundary
--------
``MemoryService`` is responsible for:
- Compatibility wrappers for memory write/mutation methods.
- Memory record queries: ``search``, ``list_memories``, ``memory_index``,
  ``list_memories_admin``
- Compatibility wrappers for scoring + lifecycle adjuncts.

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
``BenchmarkConversationIngestService``.

Construction is sync and cheap — no I/O, no model loading. The
orchestrator builds a single ``MemoryService`` instance in
``__init__`` so that delegate methods can blindly call
``self._memory_service.X`` without ``if None`` guards.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from opencortex.core.context import Context
from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
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

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator
    from opencortex.services.memory_scoring_service import MemoryScoringService
    from opencortex.services.memory_write_service import MemoryWriteService

logger = logging.getLogger(__name__)

_BATCH_ADD_CONCURRENCY = 8
_BATCH_ADD_TASK_CHUNK_SIZE = _BATCH_ADD_CONCURRENCY * 4


class MemoryService:
    """Compatibility facade plus memory search/listing surface.

    The service is constructed eagerly by the orchestrator and delegates to
    narrower services or orchestrator-owned subsystems via ``self._orch``.
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

    @property
    def _memory_write_service(self) -> "MemoryWriteService":
        """Lazy-built MemoryWriteService for write/mutation methods."""
        from opencortex.services.memory_write_service import MemoryWriteService

        cached = getattr(self, "_memory_write_service_instance", None)
        if cached is None:
            cached = MemoryWriteService(self)
            self._memory_write_service_instance = cached
        return cached

    # =========================================================================
    # Write / mutation facade
    # =========================================================================

    async def update(
        self,
        uri: str,
        abstract: Optional[str] = None,
        content: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        overview: Optional[str] = None,
    ) -> bool:
        """Delegate to MemoryWriteService.update."""
        return await self._memory_write_service.update(
            uri=uri,
            abstract=abstract,
            content=content,
            meta=meta,
            overview=overview,
        )

    async def remove(self, uri: str, recursive: bool = True) -> int:
        """Delegate to MemoryWriteService.remove."""
        return await self._memory_write_service.remove(uri, recursive=recursive)

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
        """Delegate to MemoryWriteService.add."""
        return await self._memory_write_service.add(
            abstract=abstract,
            content=content,
            overview=overview,
            category=category,
            parent_uri=parent_uri,
            uri=uri,
            context_type=context_type,
            is_leaf=is_leaf,
            meta=meta,
            related_uri=related_uri,
            session_id=session_id,
            dedup=dedup,
            dedup_threshold=dedup_threshold,
            embed_text=embed_text,
            defer_derive=defer_derive,
        )

    async def _check_duplicate(
        self,
        vector: List[float],
        memory_kind: str,
        merge_signature: str,
        threshold: float,
        tid: str,
        uid: str,
    ) -> Optional[Tuple[str, float]]:
        """Delegate to MemoryWriteService._check_duplicate."""
        return await self._memory_write_service._check_duplicate(
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
        """Delegate to MemoryWriteService._merge_into."""
        await self._memory_write_service._merge_into(
            existing_uri=existing_uri,
            new_abstract=new_abstract,
            new_content=new_content,
        )

    async def _ensure_parent_records(self, parent_uri: str) -> None:
        """Delegate to MemoryWriteService._ensure_parent_records."""
        await self._memory_write_service._ensure_parent_records(parent_uri)

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
        """Delegate to MemoryWriteService._add_document."""
        return await self._memory_write_service._add_document(
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
        """Delegate to MemoryWriteService.batch_add."""
        return await self._memory_write_service.batch_add(
            items=items,
            source_path=source_path,
            scan_meta=scan_meta,
        )

    # =========================================================================
    # Scoring + lifecycle facade
    # =========================================================================

    @property
    def _memory_scoring_service(self) -> "MemoryScoringService":
        """Lazy-built service for feedback, decay, and protection methods."""
        from opencortex.services.memory_scoring_service import MemoryScoringService

        cached = getattr(self, "_memory_scoring_service_instance", None)
        if cached is None:
            cached = MemoryScoringService(self)
            self._memory_scoring_service_instance = cached
        return cached

    async def feedback(self, uri: str, reward: float) -> None:
        """Delegate to MemoryScoringService.feedback."""
        await self._memory_scoring_service.feedback(uri, reward)

    async def feedback_batch(self, rewards: List[Dict[str, Any]]) -> None:
        """Delegate to MemoryScoringService.feedback_batch."""
        await self._memory_scoring_service.feedback_batch(rewards)

    async def decay(self) -> Optional[Dict[str, Any]]:
        """Delegate to MemoryScoringService.decay."""
        return await self._memory_scoring_service.decay()

    async def cleanup_expired_staging(self) -> int:
        """Delegate to MemoryScoringService.cleanup_expired_staging."""
        return await self._memory_scoring_service.cleanup_expired_staging()

    async def protect(self, uri: str, protected: bool = True) -> None:
        """Delegate to MemoryScoringService.protect."""
        await self._memory_scoring_service.protect(uri, protected=protected)

    async def get_profile(self, uri: str) -> Optional[Dict[str, Any]]:
        """Delegate to MemoryScoringService.get_profile."""
        return await self._memory_scoring_service.get_profile(uri)

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
            detail_level.value
            if isinstance(detail_level, DetailLevel)
            else detail_level
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
        intent_ms = stage_timings.snapshot()["probe"] + stage_timings.snapshot()["plan"]

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
        retrieve_breakdown_ms = MemoryService._summarize_retrieve_breakdown(
            query_results
        )

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
        retrieval_latency_ms = max(timing_snapshot["retrieve"], 0) + max(
            timing_snapshot.get("hydrate", 0), 0
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
                    query,
                    tid,
                    uid,
                    top_k=3,
                )
                for sr in skill_results:
                    result.skills.append(
                        MatchedContext(
                            uri=sr.uri,
                            context_type=ContextType.SKILL,
                            is_leaf=True,
                            abstract=sr.abstract,
                            overview=sr.overview,
                            content=sr.content,
                            category=sr.category.value,
                            score=0.0,
                            session_id="",
                        )
                    )
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
        if "/memories/" in uri:
            return ContextType.MEMORY
        if "/shared/cases/" in uri:
            return ContextType.CASE
        if "/shared/patterns/" in uri:
            return ContextType.PATTERN
        if "/skills/" in uri:
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
