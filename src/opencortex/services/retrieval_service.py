# SPDX-License-Identifier: Apache-2.0
"""Search and retrieval domain service for OpenCortex.

This module owns probe/planner/runtime binding, object-aware retrieval,
reranking, and session-aware search. The orchestrator keeps thin
compatibility wrappers for existing callers and tests.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.core.message import Message
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
    anchor_rerank_bonus,
    build_probe_scope_input,
    build_scope_filter,
    build_start_point_filter,
    merge_filter_clauses,
    record_anchor_groups,
)
from opencortex.intent.retrieval_support import (
    probe_candidate_ranks as build_probe_candidate_ranks,
)
from opencortex.intent.retrieval_support import (
    query_anchor_groups as build_query_anchor_groups,
)
from opencortex.retrieve.intent_analyzer import IntentAnalyzer, LLMCompletionCallable
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
    def _analyzer(self) -> Any:
        return self._orch._analyzer

    @property
    def _llm_completion(self) -> Any:
        return self._orch._llm_completion

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
            await self._entity_index.build_for_collection(
                self._storage, collection
            )
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
        leaf_uri = str(record.get("uri", "") or "")
        if uri_path_costs is not None and leaf_uri in uri_path_costs:
            score = 1.0 - uri_path_costs[leaf_uri]
        else:
            score = float(record.get("_score", record.get("score", 0.0)) or 0.0)
        reasons: List[str] = []
        target_kinds = (
            [kind.value for kind in retrieve_plan.target_memory_kinds]
            if retrieve_plan is not None
            else []
        )
        record_kind = str(record.get("memory_kind", ""))
        if record_kind in target_kinds:
            kind_rank = target_kinds.index(record_kind)
            score += 0.14 * (len(target_kinds) - kind_rank) / max(len(target_kinds), 1)
            reasons.append("kind")

        anchor_bonus, anchor_reasons = anchor_rerank_bonus(
            query_anchor_groups=query_anchor_groups,
            record_anchor_groups=record_anchor_groups(record),
        )
        if anchor_bonus > 0:
            score += anchor_bonus
            reasons.extend(anchor_reasons)

        probe_rank = probe_candidate_ranks.get(str(record.get("uri", "") or ""))
        if probe_rank is not None:
            score += max(0.04, 0.14 - min(probe_rank, 5) * 0.02)
            reasons.append("probe")

        if typed_query.target_directories and any(
            str(record.get("uri", "")).startswith(prefix)
            for prefix in typed_query.target_directories
        ):
            score += 0.06
            reasons.append("scope")

        if typed_query.target_doc_id and (
            str(record.get("source_doc_id", "")) == typed_query.target_doc_id
        ):
            score += 0.08
            reasons.append("doc")

        reward = float(record.get("reward_score", 0.0) or 0.0)
        if reward:
            score += max(min(0.06, reward * 0.03), -0.03)
            reasons.append("reward")

        active_count = int(record.get("active_count", 0) or 0)
        if active_count > 0:
            score += min(0.05, math.log1p(active_count) * 0.01)
            reasons.append("hot")

        cone_bonus = float(record.get("_cone_bonus", 0.0) or 0.0)
        if cone_weight > 0.0 and cone_bonus > 0.0:
            score += min(0.30, cone_weight * min(1.0, cone_bonus))
            reasons.append("cone")

        return score, ",".join(reasons) or "semantic"

    @staticmethod
    def _record_passes_acl(
        record: Dict[str, Any],
        tenant_id: str,
        user_id: str,
        project_id: str,
    ) -> bool:
        """Return True if record passes tenant/scope/project access control."""
        r_tenant = str(record.get("source_tenant_id", "") or "")
        if tenant_id and r_tenant and r_tenant != tenant_id:
            return False
        if record.get("scope") == "private" and record.get("source_user_id") != user_id:
            return False
        r_project = str(record.get("project_id", "") or "")
        if (
            project_id
            and project_id != "public"
            and r_project not in (project_id, "public", "")
        ):
            return False
        return True

    @staticmethod
    def _matched_record_anchors(
        *,
        record: Dict[str, Any],
        query_anchor_groups: Dict[str, set[str]],
    ) -> List[str]:
        """Return normalized query anchors that concretely matched this record."""
        if not query_anchor_groups:
            return []
        matched: List[str] = []
        record_groups = record_anchor_groups(record)
        for kind, query_values in query_anchor_groups.items():
            record_values = record_groups.get(kind, set())
            for value in sorted(query_values.intersection(record_values)):
                if value not in matched:
                    matched.append(value)
        return matched[:8]

    async def _records_to_matched_contexts(
        self,
        *,
        candidates: List[Dict[str, Any]],
        context_type: ContextType,
        detail_level: DetailLevel,
    ) -> List[MatchedContext]:
        """Convert raw store records into MatchedContext objects."""

        async def _build_one(record: Dict[str, Any]) -> MatchedContext:
            uri = str(record.get("uri", ""))
            overview = None
            if detail_level in (DetailLevel.L1, DetailLevel.L2):
                overview = str(record.get("overview", "") or "") or None

            content = None
            if detail_level == DetailLevel.L2:
                content = str(record.get("content", "") or "") or None
                if content is None and self._fs:
                    try:
                        content = await self._fs.read_file(f"{uri}/content.md")
                    except Exception:
                        content = None

            effective_type = context_type
            if context_type == ContextType.ANY:
                try:
                    effective_type = ContextType(str(record.get("context_type", "memory")))
                except ValueError:
                    effective_type = ContextType.MEMORY

            return MatchedContext(
                uri=uri,
                context_type=effective_type,
                is_leaf=bool(record.get("is_leaf", False)),
                abstract=str(record.get("abstract", "") or ""),
                overview=overview,
                content=content,
                keywords=str(record.get("keywords", "") or ""),
                category=str(record.get("category", "") or ""),
                score=float(
                    record.get("_final_score", record.get("_score", 0.0)) or 0.0
                ),
                match_reason=str(record.get("_match_reason", "") or ""),
                session_id=str(record.get("session_id", "") or ""),
                source_doc_id=record.get("source_doc_id"),
                source_doc_title=record.get("source_doc_title"),
                source_section_path=record.get("source_section_path"),
                source_uri=(
                    dict(record.get("meta") or {}).get("source_uri")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                msg_range=(
                    dict(record.get("meta") or {}).get("msg_range")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                recomposition_stage=(
                    dict(record.get("meta") or {}).get("recomposition_stage")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                layer=(
                    dict(record.get("meta") or {}).get("layer")
                    if isinstance(record.get("meta"), dict)
                    else None
                ),
                matched_anchors=list(record.get("_matched_anchors", []) or []),
                cone_used=bool(record.get("_cone_used", False)),
                path_source=record.get("_path_source") or None,
                path_cost=(
                    float(record["_path_cost"])
                    if record.get("_path_cost") is not None
                    else None
                ),
                path_breakdown=record.get("_path_breakdown") or None,
                relations=[],
            )

        return list(await asyncio.gather(*[_build_one(record) for record in candidates]))

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

        kind_filter = None
        if retrieve_plan is not None and retrieve_plan.target_memory_kinds:
            kind_filter = {
                "op": "must",
                "field": "memory_kind",
                "conds": [kind.value for kind in retrieve_plan.target_memory_kinds],
            }

        start_point_filter = build_start_point_filter(
            retrieve_plan=retrieve_plan,
            probe_result=probe_result,
            bound_plan=bound_plan,
        )

        scope_only_filter: Optional[Dict[str, Any]] = None
        if retrieve_plan is not None:
            if retrieve_plan.scope_filter:
                scope_only_filter = retrieve_plan.scope_filter
            elif retrieve_plan.scope_level != ScopeLevel.GLOBAL:
                if probe_result and probe_result.starting_points:
                    if retrieve_plan.scope_level == ScopeLevel.CONTAINER_SCOPED:
                        parent_uris = [
                            sp.uri for sp in probe_result.starting_points if sp.uri
                        ]
                        if parent_uris:
                            scope_only_filter = {
                                "op": "must",
                                "field": "parent_uri",
                                "conds": parent_uris,
                            }
                    elif retrieve_plan.scope_level == ScopeLevel.SESSION_ONLY:
                        session_ids = sorted({
                            sp.session_id
                            for sp in probe_result.starting_points
                            if sp.session_id
                        })
                        if session_ids:
                            scope_only_filter = {
                                "op": "must",
                                "field": "session_id",
                                "conds": session_ids,
                            }
                    elif retrieve_plan.scope_level == ScopeLevel.DOCUMENT_ONLY:
                        doc_ids = sorted({
                            sp.source_doc_id
                            for sp in probe_result.starting_points
                            if sp.source_doc_id
                        })
                        if doc_ids:
                            scope_only_filter = {
                                "op": "must",
                                "field": "source_doc_id",
                                "conds": doc_ids,
                            }

        is_leaf_filter = {"op": "must", "field": "is_leaf", "conds": [True]}
        leaf_filter_merged = merge_filter_clauses(
            search_filter,
            kind_filter,
            scope_only_filter,
            is_leaf_filter,
            start_point_filter,
        )
        anchor_filter_merged = merge_filter_clauses(
            search_filter,
            start_point_filter,
            scope_only_filter,
            {"op": "must", "field": "retrieval_surface", "conds": ["anchor_projection"]},
        )
        fp_filter_merged = merge_filter_clauses(
            search_filter,
            start_point_filter,
            scope_only_filter,
            {"op": "must", "field": "retrieval_surface", "conds": ["fact_point"]},
        )

        query_anchor_groups = build_query_anchor_groups(retrieve_plan, probe_result)
        rerank_enabled = bool(query_anchor_groups) or bool(
            retrieve_plan is not None and retrieve_plan.search_profile.rerank
        )
        candidate_limit = int((bound_plan or {}).get("raw_candidate_cap") or 0)
        if candidate_limit <= 0:
            recall_budget = (
                retrieve_plan.search_profile.recall_budget
                if retrieve_plan is not None
                else 0.4
            )
            candidate_limit = max(
                limit, min(64, limit + max(4, int(round(recall_budget * 20))))
            )
            if rerank_enabled:
                candidate_limit = min(64, candidate_limit + 8)

        leaf_limit = candidate_limit
        anchor_limit = min(64, candidate_limit * 2)
        fp_limit = min(96, candidate_limit * 3)

        search_results = await asyncio.gather(
            self._storage.search(
                self._get_collection(),
                query_vector=query_vector,
                filter=leaf_filter_merged,
                limit=leaf_limit,
                text_query=typed_query.query,
            ),
            self._storage.search(
                self._get_collection(),
                query_vector=query_vector,
                filter=anchor_filter_merged,
                limit=anchor_limit,
                text_query=None,
            ),
            self._storage.search(
                self._get_collection(),
                query_vector=query_vector,
                filter=fp_filter_merged,
                limit=fp_limit,
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
        fp_hits: List[Dict[str, Any]] = (
            search_results[2] if not isinstance(search_results[2], Exception) else []
        )
        if isinstance(search_results[0], Exception):
            logger.debug("[RetrievalService] leaf search failed: %s", search_results[0])
        if isinstance(search_results[1], Exception):
            logger.debug("[RetrievalService] anchor search failed: %s", search_results[1])
        if isinstance(search_results[2], Exception):
            logger.debug("[RetrievalService] fp search failed: %s", search_results[2])

        search_finished = time.perf_counter()

        def _get_target_uri(hit: Dict[str, Any]) -> str:
            return str(
                hit.get("projection_target_uri")
                or (hit.get("meta") or {}).get("projection_target_uri", "")
                or ""
            )

        known_leaf_uris = {str(r.get("uri", "")) for r in leaf_hits if r.get("uri")}
        projected_uris = {
            _get_target_uri(h)
            for h in anchor_hits + fp_hits
            if _get_target_uri(h)
        }
        missing_uris = [u for u in projected_uris if u and u not in known_leaf_uris]

        if missing_uris:
            try:
                tid, uid = get_effective_identity()
                project_id = get_effective_project_id()
                missing_filter = merge_filter_clauses(
                    search_filter,
                    scope_only_filter,
                    is_leaf_filter,
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

        uri_path_costs = compute_uri_path_scores(leaf_hits, anchor_hits, fp_hits)

        def _determine_path_source(leaf_uri: str) -> tuple[str, Optional[float]]:
            if leaf_uri not in uri_path_costs:
                return "direct", None
            cost = uri_path_costs[leaf_uri]
            best_fp_cost = None
            for hit in fp_hits:
                target_uri = _get_target_uri(hit)
                if target_uri != leaf_uri:
                    continue
                score = max(0.0, min(1.0, float(hit.get("_score", 0.0))))
                distance = 1.0 - score
                hop = (
                    URI_HOP_COST * HIGH_CONFIDENCE_DISCOUNT
                    if distance < HIGH_CONFIDENCE_THRESHOLD
                    else URI_HOP_COST
                )
                fp_cost = distance + hop
                if best_fp_cost is None or fp_cost < best_fp_cost:
                    best_fp_cost = fp_cost
            if best_fp_cost is not None and abs(best_fp_cost - cost) < 1e-9:
                return "fact_point", cost

            best_anchor_cost = None
            for hit in anchor_hits:
                target_uri = _get_target_uri(hit)
                if target_uri != leaf_uri:
                    continue
                score = max(0.0, min(1.0, float(hit.get("_score", 0.0))))
                anchor_cost = (1.0 - score) + URI_HOP_COST
                if best_anchor_cost is None or anchor_cost < best_anchor_cost:
                    best_anchor_cost = anchor_cost
            if best_anchor_cost is not None and abs(best_anchor_cost - cost) < 1e-9:
                return "anchor", cost
            return "direct", cost

        rerank_started = search_finished
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
            path_src, path_cst = _determine_path_source(leaf_uri)
            rescored_record = dict(record)
            rescored_record["_final_score"] = final_score
            rescored_record["_match_reason"] = match_reason
            rescored_record["_matched_anchors"] = self._orch._matched_record_anchors(
                record=record,
                query_anchor_groups=query_anchor_groups,
            )
            rescored_record["_cone_used"] = bool(cone_used)
            rescored_record["_path_source"] = path_src
            rescored_record["_path_cost"] = path_cst
            rescored_record["_path_breakdown"] = (
                {"uri_path_cost": path_cst, "path_source": path_src}
                if path_cst is not None
                else None
            )
            rescored.append(rescored_record)
        rescored.sort(key=lambda record: record.get("_final_score", 0.0), reverse=True)
        rerank_finished = time.perf_counter()

        matched_contexts = await self._orch._records_to_matched_contexts(
            candidates=rescored[:limit],
            context_type=typed_query.context_type,
            detail_level=typed_query.detail_level,
        )
        assembled = time.perf_counter()

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
            candidates_before_rerank=len(records),
            candidates_after_rerank=len(matched_contexts),
            frontier_waves=frontier_waves,
            frontier_budget_exceeded=False,
            total_ms=(assembled - started) * 1000,
        )
        return result

    async def session_search(
        self,
        query: str,
        messages: Optional[List[Message]] = None,
        session_summary: str = "",
        context_type: Optional[ContextType] = None,
        target_uri: str = "",
        limit: int = 5,
        score_threshold: Optional[float] = None,
        metadata_filter: Optional[Dict[str, Any]] = None,
        llm_completion: Optional[LLMCompletionCallable] = None,
    ) -> FindResult:
        """Session-aware search using IntentAnalyzer for query planning."""
        self._ensure_init()

        completion_fn = llm_completion or self._llm_completion
        if not completion_fn:
            raise ValueError(
                "session_search requires an LLM callable. "
                "Provide one via constructor or llm_completion parameter."
            )

        analyzer = self._analyzer or IntentAnalyzer(llm_completion=completion_fn)

        target_abstract = ""
        if target_uri:
            try:
                target_abstract = await self._fs.abstract(target_uri)
            except Exception:
                pass

        query_plan = await analyzer.analyze(
            compression_summary=session_summary,
            messages=messages or [],
            current_message=query,
            context_type=context_type,
            target_abstract=target_abstract,
            llm_completion=completion_fn,
        )

        if target_uri:
            for typed_query in query_plan.queries:
                typed_query.target_directories = [target_uri]

        search_filter = self._orch._build_search_filter(metadata_filter=metadata_filter)

        query_results = await asyncio.gather(
            *[
                self._orch._execute_object_query(
                    typed_query=typed_query,
                    limit=limit,
                    score_threshold=score_threshold,
                    search_filter=search_filter,
                    retrieve_plan=None,
                    probe_result=None,
                )
                for typed_query in query_plan.queries
            ]
        )

        result = self._orch._aggregate_results(query_results, limit=limit)
        result.query_plan = query_plan
        result.query_results = list(query_results)
        return result

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
