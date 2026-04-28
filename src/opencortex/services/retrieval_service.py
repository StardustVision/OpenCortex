# SPDX-License-Identifier: Apache-2.0
"""Search and retrieval domain service for OpenCortex.

This module owns probe/planner/runtime binding, object-aware retrieval,
and reranking. The orchestrator keeps thin compatibility wrappers for
existing callers and tests.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.intent import (
    QueryAnchorKind,
    RetrievalPlan,
    SearchResult,
)
from opencortex.intent.retrieval_support import (
    build_probe_scope_input,
    build_scope_filter,
)
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    FindResult,
    MatchedContext,
    QueryResult,
    TypedQuery,
)

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator
    from opencortex.services.retrieval_candidate_service import (
        RetrievalCandidateService,
    )
    from opencortex.services.retrieval_object_query_service import (
        RetrievalObjectQueryService,
    )

class RetrievalService:
    """Own search/retrieve logic while using orchestrator-owned subsystems."""

    def __init__(self, orchestrator: MemoryOrchestrator) -> None:
        self._orch = orchestrator

    @property
    def _config(self) -> Any:
        return self._orch._config

    @property
    def _storage(self) -> Any:
        return self._orch._storage

    @property
    def _embedder(self) -> Any:
        return self._orch._embedder

    @property
    def _fs(self) -> Any:
        return self._orch._fs

    @property
    def _memory_probe(self) -> Any:
        return self._orch._memory_probe

    @property
    def _recall_planner(self) -> Any:
        return self._orch._recall_planner

    @property
    def _memory_runtime(self) -> Any:
        return self._orch._memory_runtime

    @property
    def _cone_scorer(self) -> Any:
        return self._orch._cone_scorer

    @property
    def _entity_index(self) -> Any:
        return self._orch._entity_index

    @property
    def _retrieval_candidate_service(self) -> "RetrievalCandidateService":
        """Lazy-built service for candidate scoring/projection helpers."""
        from opencortex.services.retrieval_candidate_service import (
            RetrievalCandidateService,
        )

        cached = getattr(self, "_retrieval_candidate_service_instance", None)
        if cached is None:
            cached = RetrievalCandidateService(self)
            self._retrieval_candidate_service_instance = cached
        return cached

    @property
    def _retrieval_object_query_service(self) -> "RetrievalObjectQueryService":
        """Lazy-built service for object-query execution."""
        from opencortex.services.retrieval_object_query_service import (
            RetrievalObjectQueryService,
        )

        cached = getattr(self, "_retrieval_object_query_service_instance", None)
        if cached is None:
            cached = RetrievalObjectQueryService(self)
            self._retrieval_object_query_service_instance = cached
        return cached

    def _ensure_init(self) -> None:
        self._orch._ensure_init()

    def _get_collection(self) -> str:
        return self._orch._get_collection()

    async def probe_memory(
        self,
        query: str,
        *,
        context_type: Optional[ContextType] = None,
        target_uri: str = "",
        target_doc_id: Optional[str] = None,
        session_context: Optional[Dict[str, Any]] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> SearchResult:
        """Run the Phase 1 bootstrap probe."""
        self._ensure_init()
        if self._memory_probe is None:
            raise RuntimeError("memory probe is not initialized")
        scope_input = build_probe_scope_input(
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
        )
        scope_filter = build_scope_filter(
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
            metadata_filter=metadata_filter,
        )
        return await self._memory_probe.probe(
            query,
            scope_filter=scope_filter,
            scope_input=scope_input,
        )

    def memory_probe_mode(self) -> str:
        """Return the active probe backend."""
        if self._memory_probe is None:
            return "unavailable"
        return self._memory_probe.mode

    def memory_probe_trace(self) -> Dict[str, Any]:
        """Return machine-readable attribution for the last probe call."""
        if self._memory_probe is None:
            return {}
        return self._memory_probe.probe_trace()

    def plan_memory(
        self,
        *,
        query: str,
        probe_result: SearchResult,
        max_items: int,
        recall_mode: str,
        detail_level_override: Optional[str],
        scope_input: Optional[Any] = None,
    ) -> Optional[RetrievalPlan]:
        """Run the Phase 2 evidence-driven planner."""
        return self._recall_planner.semantic_plan(
            query=query,
            probe_result=probe_result,
            max_items=max_items,
            recall_mode=recall_mode,
            detail_level_override=detail_level_override,
            scope_input=scope_input,
        )

    def bind_memory_runtime(
        self,
        *,
        probe_result: SearchResult,
        retrieve_plan: RetrievalPlan,
        max_items: int,
        session_context: Optional[Dict[str, Any]],
        include_knowledge: bool,
    ) -> Dict[str, Any]:
        """Run the Phase 3 runtime binder."""
        tid, uid = get_effective_identity()
        return self._memory_runtime.bind(
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
            max_items=max_items,
            session_id=(session_context or {}).get("session_id", ""),
            tenant_id=tid,
            user_id=uid,
            project_id=get_effective_project_id(),
            include_knowledge=include_knowledge,
        )

    def _build_search_filter(
        self,
        *,
        category_filter: Optional[List[str]] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build the shared search filter used by probe and retrieval."""
        tid, uid = get_effective_identity()

        staging_exclude = {
            "op": "must_not",
            "field": "context_type",
            "conds": ["staging"],
        }
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

        combined_conds = [staging_exclude, scope_filter]
        if tid:
            combined_conds.append(
                {
                    "op": "must",
                    "field": "source_tenant_id",
                    "conds": [tid, ""],
                }
            )

        project_id = get_effective_project_id()
        if project_id and project_id != "public":
            combined_conds.append(
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

        if category_filter:
            combined_conds.append(
                {"op": "must", "field": "category", "conds": list(category_filter)}
            )

        combined_conds.append(
            {"op": "must_not", "field": "meta.superseded", "conds": [True]}
        )

        if metadata_filter:
            return {"op": "and", "conds": [metadata_filter] + combined_conds}
        return {"op": "and", "conds": combined_conds}

    def _build_probe_filter(self) -> Dict[str, Any]:
        """Return the bounded Phase 1 probe filter."""
        return self._orch._build_search_filter()

    def _cone_query_entities(
        self,
        *,
        typed_query: TypedQuery,
        query_anchor_groups: Dict[str, set[str]],
        records: List[Dict[str, Any]],
    ) -> set[str]:
        """Choose bounded query entities for cone expansion and scoring."""
        entities = set(query_anchor_groups.get(QueryAnchorKind.ENTITY.value, set()))
        if entities:
            return set(sorted(entities, key=len, reverse=True)[:6])
        if self._cone_scorer is None:
            return set()
        extracted = self._cone_scorer.extract_query_entities(
            typed_query.query,
            records,
            self._get_collection(),
        )
        if not extracted:
            return set()
        return set(sorted(extracted, key=len, reverse=True)[:6])

    async def _apply_cone_rerank(
        self,
        *,
        typed_query: TypedQuery,
        retrieve_plan: Optional[RetrievalPlan],
        query_anchor_groups: Dict[str, set[str]],
        records: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], bool]:
        """Apply lightweight cone expansion/scoring over the dense candidate pool."""
        if (
            self._cone_scorer is None
            or self._entity_index is None
            or retrieve_plan is None
            or retrieve_plan.search_profile.association_budget <= 0.0
            or not records
        ):
            return records, False

        collection = self._get_collection()
        if not self._entity_index.is_ready(collection):
            await self._entity_index.build_for_collection(self._storage, collection)
        if not self._entity_index.is_ready(collection):
            return records, False

        query_entities = self._orch._cone_query_entities(
            typed_query=typed_query,
            query_anchor_groups=query_anchor_groups,
            records=records,
        )
        if not query_entities:
            return records, False

        tid, uid = get_effective_identity()
        project_id = get_effective_project_id()
        cone_candidates = [dict(record) for record in records]
        cone_candidates.sort(
            key=lambda record: float(
                record.get("_score", record.get("score", 0.0)) or 0.0
            ),
            reverse=True,
        )
        cone_candidates = await self._cone_scorer.expand_candidates(
            cone_candidates,
            query_entities,
            self._get_collection(),
            self._storage,
            tenant_id=tid,
            user_id=uid,
            project_id=project_id,
        )
        cone_candidates = self._cone_scorer.compute_cone_scores(
            cone_candidates,
            query_entities,
            self._get_collection(),
        )
        return cone_candidates, True

    async def _embed_retrieval_query(self, query_text: str) -> Optional[List[float]]:
        """Embed one retrieval query for dense search."""
        if not self._embedder or not query_text:
            return None
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                self._embedder.embed_query,
                query_text,
            )
        except Exception:
            return None
        return getattr(result, "dense_vector", None)

    def _score_object_record(
        self,
        *,
        record: Dict[str, Any],
        typed_query: TypedQuery,
        retrieve_plan: Optional[RetrievalPlan],
        query_anchor_groups: Dict[str, set[str]],
        probe_candidate_ranks: Dict[str, int],
        cone_weight: float,
        uri_path_costs: Optional[Dict[str, float]] = None,
    ) -> tuple[float, str]:
        """Fuse URI path score (primary) with object-aware boosts."""
        return self._retrieval_candidate_service._score_object_record(
            record=record,
            typed_query=typed_query,
            retrieve_plan=retrieve_plan,
            query_anchor_groups=query_anchor_groups,
            probe_candidate_ranks=probe_candidate_ranks,
            cone_weight=cone_weight,
            uri_path_costs=uri_path_costs,
        )

    @staticmethod
    def _record_passes_acl(
        record: Dict[str, Any],
        tenant_id: str,
        user_id: str,
        project_id: str,
    ) -> bool:
        """Return True if record passes tenant/scope/project access control."""
        from opencortex.services.retrieval_candidate_service import (
            RetrievalCandidateService,
        )

        return RetrievalCandidateService._record_passes_acl(
            record,
            tenant_id,
            user_id,
            project_id,
        )

    @staticmethod
    def _matched_record_anchors(
        *,
        record: Dict[str, Any],
        query_anchor_groups: Dict[str, set[str]],
    ) -> List[str]:
        """Return normalized query anchors that concretely matched this record."""
        from opencortex.services.retrieval_candidate_service import (
            RetrievalCandidateService,
        )

        return RetrievalCandidateService._matched_record_anchors(
            record=record,
            query_anchor_groups=query_anchor_groups,
        )

    async def _records_to_matched_contexts(
        self,
        *,
        candidates: List[Dict[str, Any]],
        context_type: ContextType,
        detail_level: DetailLevel,
    ) -> List[MatchedContext]:
        """Convert raw store records into MatchedContext objects."""
        return await self._retrieval_candidate_service._records_to_matched_contexts(
            candidates=candidates,
            context_type=context_type,
            detail_level=detail_level,
        )

    async def _execute_object_query(
        self,
        *,
        typed_query: TypedQuery,
        limit: int,
        score_threshold: Optional[float],
        search_filter: Optional[Dict[str, Any]],
        retrieve_plan: Optional[RetrievalPlan],
        probe_result: Optional[SearchResult],
        bound_plan: Optional[Dict[str, Any]] = None,
    ) -> QueryResult:
        """Execute one object-aware retrieval query with three-layer parallel search."""
        return await self._retrieval_object_query_service._execute_object_query(
            typed_query=typed_query,
            limit=limit,
            score_threshold=score_threshold,
            search_filter=search_filter,
            retrieve_plan=retrieve_plan,
            probe_result=probe_result,
            bound_plan=bound_plan,
        )

    def _aggregate_results(
        self,
        query_results: List[QueryResult],
        *,
        limit: Optional[int] = None,
    ) -> FindResult:
        """Aggregate multiple QueryResults into a single FindResult."""
        ranked_contexts = []
        seen_uris: set = set()

        for result in query_results:
            for ctx in result.matched_contexts:
                if ctx.uri in seen_uris:
                    continue
                seen_uris.add(ctx.uri)
                ranked_contexts.append(ctx)

        ranked_contexts.sort(
            key=lambda ctx: float(getattr(ctx, "score", 0.0) or 0.0),
            reverse=True,
        )
        if limit is not None:
            ranked_contexts = ranked_contexts[: max(limit, 0)]

        memories, resources, skills = [], [], []
        for ctx in ranked_contexts:
            if ctx.context_type in (
                ContextType.MEMORY,
                ContextType.CASE,
                ContextType.PATTERN,
            ):
                memories.append(ctx)
            elif ctx.context_type == ContextType.RESOURCE:
                resources.append(ctx)
            elif ctx.context_type == ContextType.SKILL:
                skills.append(ctx)
            else:
                memories.append(ctx)

        return FindResult(
            memories=memories,
            resources=resources,
            skills=skills,
        )
