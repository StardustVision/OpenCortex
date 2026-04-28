# SPDX-License-Identifier: Apache-2.0
"""Search and retrieval domain service for OpenCortex.

This module owns probe/planner/runtime binding, object-aware retrieval,
and reranking. The orchestrator keeps thin compatibility wrappers for
existing callers and tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.http.request_context import (
    get_effective_identity,
    get_effective_project_id,
)
from opencortex.intent import (
    QueryAnchorKind,
    RetrievalPlan,
    ScopeLevel,
    SearchResult,
)
from opencortex.intent.retrieval_support import (
    build_probe_scope_input,
    build_scope_filter,
    build_start_point_filter,
    merge_filter_clauses,
)
from opencortex.intent.retrieval_support import (
    probe_candidate_ranks as build_probe_candidate_ranks,
)
from opencortex.intent.retrieval_support import (
    query_anchor_groups as build_query_anchor_groups,
)
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    FindResult,
    MatchedContext,
    QueryResult,
    SearchExplain,
    TypedQuery,
)
from opencortex.retrieve.uri_path_scorer import (
    HIGH_CONFIDENCE_DISCOUNT,
    HIGH_CONFIDENCE_THRESHOLD,
    URI_HOP_COST,
    compute_uri_path_scores,
)

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator
    from opencortex.services.retrieval_candidate_service import (
        RetrievalCandidateService,
    )

logger = logging.getLogger(__name__)


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

    @staticmethod
    def _object_query_kind_filter(
        retrieve_plan: Optional[RetrievalPlan],
    ) -> Optional[Dict[str, Any]]:
        """Build memory-kind narrowing for one object query."""
        if retrieve_plan is None or not retrieve_plan.target_memory_kinds:
            return None
        return {
            "op": "must",
            "field": "memory_kind",
            "conds": [kind.value for kind in retrieve_plan.target_memory_kinds],
        }

    @staticmethod
    def _object_query_scope_filter(
        retrieve_plan: Optional[RetrievalPlan],
        probe_result: Optional[SearchResult],
    ) -> Optional[Dict[str, Any]]:
        """Build planner-selected scope narrowing for one object query."""
        if retrieve_plan is None:
            return None
        if retrieve_plan.scope_filter:
            return retrieve_plan.scope_filter
        if retrieve_plan.scope_level == ScopeLevel.GLOBAL:
            return None
        if not probe_result or not probe_result.starting_points:
            return None

        if retrieve_plan.scope_level == ScopeLevel.CONTAINER_SCOPED:
            parent_uris = [sp.uri for sp in probe_result.starting_points if sp.uri]
            if parent_uris:
                return {"op": "must", "field": "parent_uri", "conds": parent_uris}
        elif retrieve_plan.scope_level == ScopeLevel.SESSION_ONLY:
            session_ids = sorted(
                {sp.session_id for sp in probe_result.starting_points if sp.session_id}
            )
            if session_ids:
                return {"op": "must", "field": "session_id", "conds": session_ids}
        elif retrieve_plan.scope_level == ScopeLevel.DOCUMENT_ONLY:
            doc_ids = sorted(
                {
                    sp.source_doc_id
                    for sp in probe_result.starting_points
                    if sp.source_doc_id
                }
            )
            if doc_ids:
                return {"op": "must", "field": "source_doc_id", "conds": doc_ids}
        return None

    @staticmethod
    def _object_query_candidate_limit(
        *,
        limit: int,
        retrieve_plan: Optional[RetrievalPlan],
        bound_plan: Optional[Dict[str, Any]],
        rerank_enabled: bool,
    ) -> int:
        """Resolve the raw candidate cap for one object query."""
        candidate_limit = int((bound_plan or {}).get("raw_candidate_cap") or 0)
        if candidate_limit > 0:
            return candidate_limit

        recall_budget = (
            retrieve_plan.search_profile.recall_budget
            if retrieve_plan is not None
            else 0.4
        )
        candidate_limit = max(
            limit,
            min(64, limit + max(4, int(round(recall_budget * 20)))),
        )
        if rerank_enabled:
            candidate_limit = min(64, candidate_limit + 8)
        return candidate_limit

    @staticmethod
    def _projection_target_uri(hit: Dict[str, Any]) -> str:
        """Return the leaf URI targeted by an anchor/fact projection."""
        return str(
            hit.get("projection_target_uri")
            or (hit.get("meta") or {}).get("projection_target_uri", "")
            or ""
        )

    def _object_query_filters(
        self,
        *,
        search_filter: Optional[Dict[str, Any]],
        retrieve_plan: Optional[RetrievalPlan],
        probe_result: Optional[SearchResult],
        bound_plan: Optional[Dict[str, Any]],
    ) -> tuple[
        Dict[str, Any],
        Dict[str, Any],
        Dict[str, Any],
        Optional[Dict[str, Any]],
    ]:
        """Build leaf, anchor, fact-point, and planner scope filters."""
        kind_filter = self._object_query_kind_filter(retrieve_plan)
        start_point_filter = build_start_point_filter(
            retrieve_plan=retrieve_plan,
            probe_result=probe_result,
            bound_plan=bound_plan,
        )
        scope_only_filter = self._object_query_scope_filter(
            retrieve_plan,
            probe_result,
        )
        is_leaf_filter = {"op": "must", "field": "is_leaf", "conds": [True]}
        leaf_filter = merge_filter_clauses(
            search_filter,
            kind_filter,
            scope_only_filter,
            is_leaf_filter,
            start_point_filter,
        )
        anchor_filter = merge_filter_clauses(
            search_filter,
            start_point_filter,
            scope_only_filter,
            {
                "op": "must",
                "field": "retrieval_surface",
                "conds": ["anchor_projection"],
            },
        )
        fact_point_filter = merge_filter_clauses(
            search_filter,
            start_point_filter,
            scope_only_filter,
            {"op": "must", "field": "retrieval_surface", "conds": ["fact_point"]},
        )
        return leaf_filter, anchor_filter, fact_point_filter, scope_only_filter

    async def _search_object_layers(
        self,
        *,
        typed_query: TypedQuery,
        query_vector: Optional[List[float]],
        leaf_filter: Dict[str, Any],
        anchor_filter: Dict[str, Any],
        fact_point_filter: Dict[str, Any],
        candidate_limit: int,
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Run leaf, anchor, and fact-point searches in parallel."""
        leaf_limit = candidate_limit
        anchor_limit = min(64, candidate_limit * 2)
        fact_point_limit = min(96, candidate_limit * 3)
        search_results = await asyncio.gather(
            self._storage.search(
                self._get_collection(),
                query_vector=query_vector,
                filter=leaf_filter,
                limit=leaf_limit,
                text_query=typed_query.query,
            ),
            self._storage.search(
                self._get_collection(),
                query_vector=query_vector,
                filter=anchor_filter,
                limit=anchor_limit,
                text_query=None,
            ),
            self._storage.search(
                self._get_collection(),
                query_vector=query_vector,
                filter=fact_point_filter,
                limit=fact_point_limit,
                text_query=None,
            ),
            return_exceptions=True,
        )
        leaf_hits: List[Dict[str, Any]] = (
            search_results[0] if not isinstance(search_results[0], Exception) else []
        )
        anchor_hits: List[Dict[str, Any]] = (
            search_results[1] if not isinstance(search_results[1], Exception) else []
        )
        fact_point_hits: List[Dict[str, Any]] = (
            search_results[2] if not isinstance(search_results[2], Exception) else []
        )
        if isinstance(search_results[0], Exception):
            logger.debug("[RetrievalService] leaf search failed: %s", search_results[0])
        if isinstance(search_results[1], Exception):
            logger.debug(
                "[RetrievalService] anchor search failed: %s", search_results[1]
            )
        if isinstance(search_results[2], Exception):
            logger.debug("[RetrievalService] fp search failed: %s", search_results[2])
        return leaf_hits, anchor_hits, fact_point_hits

    async def _load_missing_projected_leaves(
        self,
        *,
        leaf_hits: List[Dict[str, Any]],
        anchor_hits: List[Dict[str, Any]],
        fact_point_hits: List[Dict[str, Any]],
        search_filter: Optional[Dict[str, Any]],
        scope_only_filter: Optional[Dict[str, Any]],
    ) -> None:
        """Hydrate leaves referenced only through projection records."""
        known_leaf_uris = {str(r.get("uri", "")) for r in leaf_hits if r.get("uri")}
        projected_uris = {
            self._projection_target_uri(hit)
            for hit in anchor_hits + fact_point_hits
            if self._projection_target_uri(hit)
        }
        missing_uris = [u for u in projected_uris if u and u not in known_leaf_uris]
        if not missing_uris:
            return

        try:
            tid, uid = get_effective_identity()
            project_id = get_effective_project_id()
            missing_filter = merge_filter_clauses(
                search_filter,
                scope_only_filter,
                {"op": "must", "field": "is_leaf", "conds": [True]},
                {"op": "must", "field": "uri", "conds": missing_uris},
            )
            loaded = await self._storage.search(
                self._get_collection(),
                query_vector=None,
                filter=missing_filter,
                limit=len(missing_uris) + 5,
            )
            for record in loaded:
                if self._orch._record_passes_acl(record, tid, uid, project_id):
                    leaf_hits.append(record)
                    known_leaf_uris.add(str(record.get("uri", "") or ""))
        except Exception as exc:
            logger.debug("[RetrievalService] batch URI load failed: %s", exc)

    @classmethod
    def _object_query_path_source(
        cls,
        *,
        leaf_uri: str,
        uri_path_costs: Dict[str, float],
        anchor_hits: List[Dict[str, Any]],
        fact_point_hits: List[Dict[str, Any]],
    ) -> tuple[str, Optional[float]]:
        """Explain whether a leaf matched directly, through anchors, or facts."""
        if leaf_uri not in uri_path_costs:
            return "direct", None
        cost = uri_path_costs[leaf_uri]

        best_fact_point_cost = None
        for hit in fact_point_hits:
            if cls._projection_target_uri(hit) != leaf_uri:
                continue
            score = max(0.0, min(1.0, float(hit.get("_score", 0.0))))
            distance = 1.0 - score
            hop = (
                URI_HOP_COST * HIGH_CONFIDENCE_DISCOUNT
                if distance < HIGH_CONFIDENCE_THRESHOLD
                else URI_HOP_COST
            )
            fact_point_cost = distance + hop
            if (
                best_fact_point_cost is None
                or fact_point_cost < best_fact_point_cost
            ):
                best_fact_point_cost = fact_point_cost
        if best_fact_point_cost is not None and abs(best_fact_point_cost - cost) < 1e-9:
            return "fact_point", cost

        best_anchor_cost = None
        for hit in anchor_hits:
            if cls._projection_target_uri(hit) != leaf_uri:
                continue
            score = max(0.0, min(1.0, float(hit.get("_score", 0.0))))
            anchor_cost = (1.0 - score) + URI_HOP_COST
            if best_anchor_cost is None or anchor_cost < best_anchor_cost:
                best_anchor_cost = anchor_cost
        if best_anchor_cost is not None and abs(best_anchor_cost - cost) < 1e-9:
            return "anchor", cost
        return "direct", cost

    async def _rescore_object_records(
        self,
        *,
        typed_query: TypedQuery,
        retrieve_plan: Optional[RetrievalPlan],
        probe_result: Optional[SearchResult],
        query_anchor_groups: Dict[str, set[str]],
        leaf_hits: List[Dict[str, Any]],
        uri_path_costs: Dict[str, float],
        anchor_hits: List[Dict[str, Any]],
        fact_point_hits: List[Dict[str, Any]],
        score_threshold: Optional[float],
    ) -> tuple[List[Dict[str, Any]], int, int]:
        """Apply cone rerank, score fusion, and path metadata."""
        frontier_waves = 0
        probe_candidate_ranks = build_probe_candidate_ranks(probe_result)
        records, cone_used = await self._orch._apply_cone_rerank(
            typed_query=typed_query,
            retrieve_plan=retrieve_plan,
            query_anchor_groups=query_anchor_groups,
            records=leaf_hits,
        )
        if cone_used:
            frontier_waves = 1

        cone_weight = 0.0
        if retrieve_plan is not None and cone_used:
            association_budget = retrieve_plan.search_profile.association_budget
            cone_weight = min(
                0.24,
                self._config.cone_weight * (0.6 + 0.8 * association_budget),
            )

        rescored: List[Dict[str, Any]] = []
        for record in records:
            leaf_uri = str(record.get("uri", "") or "")
            final_score, match_reason = self._orch._score_object_record(
                record=record,
                typed_query=typed_query,
                retrieve_plan=retrieve_plan,
                query_anchor_groups=query_anchor_groups,
                probe_candidate_ranks=probe_candidate_ranks,
                cone_weight=cone_weight,
                uri_path_costs=uri_path_costs,
            )
            if score_threshold is not None and final_score < score_threshold:
                continue
            path_source, path_cost = self._object_query_path_source(
                leaf_uri=leaf_uri,
                uri_path_costs=uri_path_costs,
                anchor_hits=anchor_hits,
                fact_point_hits=fact_point_hits,
            )
            rescored_record = dict(record)
            rescored_record["_final_score"] = final_score
            rescored_record["_match_reason"] = match_reason
            rescored_record["_matched_anchors"] = self._orch._matched_record_anchors(
                record=record,
                query_anchor_groups=query_anchor_groups,
            )
            rescored_record["_cone_used"] = bool(cone_used)
            rescored_record["_path_source"] = path_source
            rescored_record["_path_cost"] = path_cost
            rescored_record["_path_breakdown"] = (
                {"uri_path_cost": path_cost, "path_source": path_source}
                if path_cost is not None
                else None
            )
            rescored.append(rescored_record)
        rescored.sort(key=lambda record: record.get("_final_score", 0.0), reverse=True)
        return rescored, frontier_waves, len(records)

    @staticmethod
    def _object_query_result(
        *,
        typed_query: TypedQuery,
        matched_contexts: List[MatchedContext],
        started: float,
        embed_started: float,
        embed_finished: float,
        search_finished: float,
        rerank_started: float,
        rerank_finished: float,
        assembled: float,
        candidates_before_rerank: int,
        frontier_waves: int,
    ) -> QueryResult:
        """Assemble the public QueryResult and explain payload."""
        result = QueryResult(
            query=typed_query,
            matched_contexts=matched_contexts,
            searched_directories=list(typed_query.target_directories or []),
            timing_ms={
                "embed": round((embed_finished - embed_started) * 1000, 4),
                "search": round((search_finished - embed_finished) * 1000, 4),
                "rerank": round((rerank_finished - rerank_started) * 1000, 4),
                "assemble": round((assembled - rerank_finished) * 1000, 4),
                "total": round((assembled - started) * 1000, 4),
            },
        )
        result.explain = SearchExplain(
            query_class=typed_query.intent or "",
            path="object_recall",
            intent_ms=0.0,
            embed_ms=(embed_finished - embed_started) * 1000,
            search_ms=(search_finished - embed_finished) * 1000,
            rerank_ms=(rerank_finished - rerank_started) * 1000,
            assemble_ms=(assembled - rerank_finished) * 1000,
            doc_scope_hit=bool(typed_query.target_doc_id),
            time_filter_hit=False,
            candidates_before_rerank=candidates_before_rerank,
            candidates_after_rerank=len(matched_contexts),
            frontier_waves=frontier_waves,
            frontier_budget_exceeded=False,
            total_ms=(assembled - started) * 1000,
        )
        return result

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
        started = time.perf_counter()
        embed_started = started
        query_vector = await self._orch._embed_retrieval_query(typed_query.query)
        embed_finished = time.perf_counter()

        query_anchor_groups = build_query_anchor_groups(retrieve_plan, probe_result)
        rerank_enabled = bool(query_anchor_groups) or bool(
            retrieve_plan is not None and retrieve_plan.search_profile.rerank
        )
        candidate_limit = self._object_query_candidate_limit(
            limit=limit,
            retrieve_plan=retrieve_plan,
            bound_plan=bound_plan,
            rerank_enabled=rerank_enabled,
        )
        (
            leaf_filter,
            anchor_filter,
            fact_point_filter,
            scope_only_filter,
        ) = self._object_query_filters(
            search_filter=search_filter,
            retrieve_plan=retrieve_plan,
            probe_result=probe_result,
            bound_plan=bound_plan,
        )
        leaf_hits, anchor_hits, fact_point_hits = await self._search_object_layers(
            typed_query=typed_query,
            query_vector=query_vector,
            leaf_filter=leaf_filter,
            anchor_filter=anchor_filter,
            fact_point_filter=fact_point_filter,
            candidate_limit=candidate_limit,
        )
        search_finished = time.perf_counter()

        await self._load_missing_projected_leaves(
            leaf_hits=leaf_hits,
            anchor_hits=anchor_hits,
            fact_point_hits=fact_point_hits,
            search_filter=search_filter,
            scope_only_filter=scope_only_filter,
        )
        uri_path_costs = compute_uri_path_scores(
            leaf_hits,
            anchor_hits,
            fact_point_hits,
        )

        rerank_started = search_finished
        rescored, frontier_waves, candidates_before_rerank = (
            await self._rescore_object_records(
                typed_query=typed_query,
                retrieve_plan=retrieve_plan,
                probe_result=probe_result,
                query_anchor_groups=query_anchor_groups,
                leaf_hits=leaf_hits,
                uri_path_costs=uri_path_costs,
                anchor_hits=anchor_hits,
                fact_point_hits=fact_point_hits,
                score_threshold=score_threshold,
            )
        )
        rerank_finished = time.perf_counter()

        matched_contexts = await self._orch._records_to_matched_contexts(
            candidates=rescored[:limit],
            context_type=typed_query.context_type,
            detail_level=typed_query.detail_level,
        )
        assembled = time.perf_counter()

        return self._object_query_result(
            typed_query=typed_query,
            matched_contexts=matched_contexts,
            started=started,
            embed_started=embed_started,
            embed_finished=embed_finished,
            search_finished=search_finished,
            rerank_started=rerank_started,
            rerank_finished=rerank_finished,
            assembled=assembled,
            candidates_before_rerank=candidates_before_rerank,
            frontier_waves=frontier_waves,
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
