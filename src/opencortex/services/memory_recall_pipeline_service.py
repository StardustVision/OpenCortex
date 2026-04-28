# SPDX-License-Identifier: Apache-2.0
"""Recall pipeline orchestration for memory search."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from opencortex.http.request_context import get_effective_identity
from opencortex.intent import RetrievalPlan, SearchResult
from opencortex.intent.retrieval_support import (
    build_probe_scope_input,
    build_scope_filter,
)
from opencortex.intent.timing import StageTimingCollector, measure_async, measure_sync
from opencortex.retrieve.types import ContextType, DetailLevel, FindResult
from opencortex.services.memory_signals import RecallCompletedSignal

if TYPE_CHECKING:
    from opencortex.services.memory_query_service import MemoryQueryService

logger = logging.getLogger(__name__)


class MemoryRecallPipelineService:
    """Owns probe-plan-bind-retrieve recall orchestration."""

    def __init__(self, query_service: "MemoryQueryService") -> None:
        """Bind the pipeline service to a query service facade."""
        self._query_service = query_service

    @property
    def _orch(self) -> Any:
        return self._query_service._orch

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
        """Search for relevant contexts using probe-planner-runtime pipeline."""
        orch = self._orch
        orch._ensure_init()
        search_started = asyncio.get_running_loop().time()
        tenant_id, user_id = get_effective_identity()
        stage_timings = StageTimingCollector()

        target_doc_id = meta.get("target_doc_id") if isinstance(meta, dict) else None
        detail_level_override = self._detail_level_override(detail_level)
        scope_filter = build_scope_filter(
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
            metadata_filter=metadata_filter,
        )

        probe_result = await self._probe(
            stage_timings=stage_timings,
            query=query,
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
            metadata_filter=metadata_filter,
            probe_result=probe_result,
        )
        retrieve_plan = self._plan(
            stage_timings=stage_timings,
            query=query,
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
            limit=limit,
            detail_level_override=detail_level_override,
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
        )
        intent_ms = stage_timings.snapshot()["probe"] + stage_timings.snapshot()["plan"]

        if retrieve_plan is None:
            return self._empty_result(
                search_started=search_started,
                tenant_id=tenant_id,
                user_id=user_id,
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
        typed_queries = self._query_service._service._build_typed_queries(
            query=query,
            context_type=context_type,
            target_uri=target_uri,
            retrieve_plan=retrieve_plan,
            runtime_bound_plan=runtime_bound_plan,
        )
        self._bind_query_targets(
            typed_queries=typed_queries,
            target_doc_id=target_doc_id,
            target_uri=target_uri,
        )
        search_filter = orch._build_search_filter(metadata_filter=scope_filter)
        query_results = await self._retrieve(
            stage_timings=stage_timings,
            typed_queries=typed_queries,
            effective_limit=effective_limit,
            score_threshold=score_threshold,
            search_filter=search_filter,
            retrieve_plan=retrieve_plan,
            probe_result=probe_result,
            runtime_bound_plan=runtime_bound_plan,
        )
        hydration_actions: List[Dict[str, Any]] = []

        aggregate_started = asyncio.get_running_loop().time()
        result = orch._aggregate_results(query_results, limit=limit)
        result.probe_result = probe_result
        result.retrieve_plan = retrieve_plan
        retrieve_breakdown_ms = (
            self._query_service._service._summarize_retrieve_breakdown(query_results)
        )
        self._filter_leaf_results(result)
        all_matched = result.memories + result.resources + result.skills

        stage_timings.record_elapsed("aggregate", aggregate_started)
        total_ms = int((asyncio.get_running_loop().time() - search_started) * 1000)
        stage_timings.record_ms("total", total_ms)
        timing_snapshot = stage_timings.snapshot()
        retrieval_latency_ms = max(timing_snapshot["retrieve"], 0) + max(
            timing_snapshot.get("hydrate", 0), 0
        )
        overhead_ms = timing_snapshot["overhead"]
        self._finalize_runtime(
            result=result,
            runtime_bound_plan=runtime_bound_plan,
            all_matched=all_matched,
            retrieval_latency_ms=retrieval_latency_ms,
            timing_snapshot=timing_snapshot,
            retrieve_breakdown_ms=retrieve_breakdown_ms,
            hydration_actions=hydration_actions,
        )
        self._log_success(
            tenant_id=tenant_id,
            user_id=user_id,
            probe_result=probe_result,
            typed_query_count=len(typed_queries),
            matched_count=len(all_matched),
            total_ms=total_ms,
            intent_ms=intent_ms,
            retrieval_latency_ms=retrieval_latency_ms,
            overhead_ms=overhead_ms,
        )
        self._attach_explain_summary(
            result=result,
            query_results=query_results,
            total_ms=total_ms,
        )
        result.total = len(result.memories) + len(result.resources) + len(result.skills)
        self._publish_recall_completed(
            result=result,
            query=query,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        return result

    @staticmethod
    def _detail_level_override(detail_level: str) -> Optional[str]:
        """Return a planner detail-level override when not default L1."""
        detail_level_value = (
            detail_level.value
            if isinstance(detail_level, DetailLevel)
            else detail_level
        )
        return (
            detail_level_value if detail_level_value != DetailLevel.L1.value else None
        )

    async def _probe(
        self,
        *,
        stage_timings: StageTimingCollector,
        query: str,
        context_type: Optional[ContextType],
        target_uri: str,
        target_doc_id: Optional[str],
        session_context: Optional[Dict[str, Any]],
        metadata_filter: Optional[Dict[str, Any]],
        probe_result: Optional[SearchResult],
    ) -> SearchResult:
        """Run or record the probe stage."""
        if probe_result is not None:
            stage_timings.record_ms("probe", 0)
            return probe_result
        return await measure_async(
            stage_timings,
            "probe",
            self._orch.probe_memory,
            query,
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
            metadata_filter=metadata_filter,
        )

    def _plan(
        self,
        *,
        stage_timings: StageTimingCollector,
        query: str,
        context_type: Optional[ContextType],
        target_uri: str,
        target_doc_id: Optional[str],
        session_context: Optional[Dict[str, Any]],
        limit: int,
        detail_level_override: Optional[str],
        probe_result: SearchResult,
        retrieve_plan: Optional[RetrievalPlan],
    ) -> Optional[RetrievalPlan]:
        """Run or record the planner stage."""
        if retrieve_plan is not None:
            stage_timings.record_ms("plan", 0)
            return retrieve_plan

        scope_input = build_probe_scope_input(
            context_type=context_type,
            target_uri=target_uri,
            target_doc_id=target_doc_id,
            session_context=session_context,
        )
        return measure_sync(
            stage_timings,
            "plan",
            self._orch.plan_memory,
            query=query,
            probe_result=probe_result,
            max_items=limit,
            recall_mode="auto",
            detail_level_override=detail_level_override,
            scope_input=scope_input,
        )

    @staticmethod
    def _bind_query_targets(
        *,
        typed_queries: List[Any],
        target_doc_id: Optional[str],
        target_uri: str,
    ) -> None:
        """Apply target document and directory constraints to typed queries."""
        if target_doc_id:
            for typed_query in typed_queries:
                typed_query.target_doc_id = target_doc_id

        if target_uri:
            for typed_query in typed_queries:
                if not typed_query.target_directories:
                    typed_query.target_directories = [target_uri]

    async def _retrieve(
        self,
        *,
        stage_timings: StageTimingCollector,
        typed_queries: List[Any],
        effective_limit: int,
        score_threshold: Optional[float],
        search_filter: Dict[str, Any],
        retrieve_plan: RetrievalPlan,
        probe_result: SearchResult,
        runtime_bound_plan: Dict[str, Any],
    ) -> List[Any]:
        """Run object-query retrieval for all typed queries."""
        retrieval_coros = [
            self._orch._execute_object_query(
                typed_query=typed_query,
                limit=effective_limit,
                score_threshold=score_threshold,
                search_filter=search_filter,
                retrieve_plan=retrieve_plan,
                probe_result=probe_result,
                bound_plan=runtime_bound_plan,
            )
            for typed_query in typed_queries
        ]
        query_results = await measure_async(
            stage_timings,
            "retrieve",
            asyncio.gather,
            *retrieval_coros,
        )
        return list(query_results)

    @staticmethod
    def _filter_leaf_results(result: FindResult) -> None:
        """Remove directory nodes from user-facing recall results."""
        result.memories = [matched for matched in result.memories if matched.is_leaf]
        result.resources = [matched for matched in result.resources if matched.is_leaf]
        result.skills = [matched for matched in result.skills if matched.is_leaf]

    def _finalize_runtime(
        self,
        *,
        result: FindResult,
        runtime_bound_plan: Dict[str, Any],
        all_matched: List[Any],
        retrieval_latency_ms: float,
        timing_snapshot: Dict[str, float],
        retrieve_breakdown_ms: Dict[str, float],
        hydration_actions: List[Dict[str, Any]],
    ) -> None:
        """Attach runtime finalization output to the result."""
        runtime_items = [
            {
                "uri": matched.uri,
                "context_type": matched.context_type.value,
                "score": matched.score,
            }
            for matched in all_matched
        ]
        result.runtime_result = self._orch._memory_runtime.finalize(
            bound_plan=runtime_bound_plan,
            items=runtime_items,
            latency_ms=retrieval_latency_ms,
            stage_timing_ms=timing_snapshot,
            retrieve_breakdown_ms=retrieve_breakdown_ms,
            hydration_actions=hydration_actions,
        )

    @staticmethod
    def _log_success(
        *,
        tenant_id: str,
        user_id: str,
        probe_result: SearchResult,
        typed_query_count: int,
        matched_count: int,
        total_ms: int,
        intent_ms: float,
        retrieval_latency_ms: float,
        overhead_ms: float,
    ) -> None:
        """Log recall pipeline timing in the existing format."""
        logger.info(
            "[search] tenant=%s user=%s probe_candidates=%d queries=%d results=%d "
            "timing_ms(total=%d intent=%d retrieval=%d overhead=%d)",
            tenant_id,
            user_id,
            probe_result.evidence.candidate_count,
            typed_query_count,
            matched_count,
            total_ms,
            intent_ms,
            retrieval_latency_ms,
            overhead_ms,
        )

    def _attach_explain_summary(
        self,
        *,
        result: FindResult,
        query_results: List[Any],
        total_ms: int,
    ) -> None:
        """Attach the aggregate explain summary when enabled."""
        if (
            not getattr(self._orch._config, "explain_enabled", True)
            or not query_results
        ):
            return

        from opencortex.retrieve.types import SearchExplainSummary

        primary = query_results[0]
        result.explain_summary = SearchExplainSummary(
            total_ms=float(total_ms),
            query_count=len(query_results),
            primary_query_class=primary.explain.query_class if primary.explain else "",
            primary_path=primary.explain.path if primary.explain else "",
            doc_scope_hit=any(
                query_result.explain and query_result.explain.doc_scope_hit
                for query_result in query_results
            ),
            time_filter_hit=any(
                query_result.explain and query_result.explain.time_filter_hit
                for query_result in query_results
            ),
            rerank_triggered=any(
                query_result.explain and query_result.explain.rerank_ms > 0
                for query_result in query_results
            ),
        )

    def _publish_recall_completed(
        self,
        *,
        result: FindResult,
        query: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Publish recall-completed lifecycle signal when a bus exists."""
        signal_bus = getattr(self._orch, "_memory_signal_bus", None)
        if signal_bus is None:
            return
        signal_bus.publish_nowait(
            RecallCompletedSignal(
                query=query,
                tenant_id=tenant_id,
                user_id=user_id,
                memories=list(result.memories),
                resources=list(result.resources),
                skills=list(result.skills),
            )
        )

    @staticmethod
    def _empty_result(
        *,
        search_started: float,
        tenant_id: str,
        user_id: str,
        probe_result: SearchResult,
    ) -> FindResult:
        """Return the no-plan recall short-circuit result."""
        total_ms = int((asyncio.get_running_loop().time() - search_started) * 1000)
        logger.debug(
            "[search] should_recall=False tenant=%s user=%s total_ms=%d",
            tenant_id,
            user_id,
            total_ms,
        )
        return FindResult(
            memories=[],
            resources=[],
            skills=[],
            probe_result=probe_result,
        )
