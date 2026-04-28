# SPDX-License-Identifier: Apache-2.0
"""Tests for recall pipeline orchestration extraction."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from opencortex.http.request_context import reset_request_identity, set_request_identity
from opencortex.intent import RetrievalDepth, SearchResult
from opencortex.retrieve.types import (
    ContextType,
    FindResult,
    MatchedContext,
    QueryResult,
    SearchExplain,
    TypedQuery,
)
from opencortex.services.memory_query_service import MemoryQueryService
from opencortex.services.memory_recall_pipeline_service import (
    MemoryRecallPipelineService,
)
from opencortex.services.memory_signals import RecallCompletedSignal


class _SignalBus:
    """Capture published recall signals for assertions."""

    def __init__(self) -> None:
        self.signals: list[object] = []

    def publish_nowait(self, signal: object) -> None:
        self.signals.append(signal)


class TestMemoryRecallPipelineService(unittest.IsolatedAsyncioTestCase):
    """Verify the extracted recall pipeline service boundary."""

    async def test_memory_query_search_delegates_to_pipeline(self) -> None:
        """MemoryQueryService.search remains a compatibility wrapper."""
        expected = FindResult(memories=[], resources=[], skills=[])
        pipeline = SimpleNamespace(search=AsyncMock(return_value=expected))
        service = SimpleNamespace(_orch=SimpleNamespace())
        query_service = MemoryQueryService(service)
        query_service._recall_pipeline_service_instance = pipeline

        result = await query_service.search("what changed?", limit=3)

        self.assertIs(result, expected)
        pipeline.search.assert_awaited_once_with(
            query="what changed?",
            context_type=None,
            target_uri="",
            limit=3,
            score_threshold=None,
            metadata_filter=None,
            detail_level="l1",
            probe_result=None,
            retrieve_plan=None,
            meta=None,
            session_context=None,
        )

    async def test_no_plan_short_circuits_without_signal(self) -> None:
        """A planner miss returns an empty result and does not publish recall."""
        signal_bus = _SignalBus()
        orch = SimpleNamespace(
            _ensure_init=MagicMock(),
            _memory_signal_bus=signal_bus,
            plan_memory=MagicMock(return_value=None),
        )
        memory_service = SimpleNamespace(_orch=orch)
        query_service = MemoryQueryService(memory_service)
        pipeline = MemoryRecallPipelineService(query_service)
        probe_result = SearchResult(should_recall=False)
        tokens = set_request_identity("tenant", "user")
        try:
            result = await pipeline.search(
                "missing",
                probe_result=probe_result,
                retrieve_plan=None,
            )
        finally:
            reset_request_identity(tokens)

        self.assertEqual(result.memories, [])
        self.assertEqual(result.resources, [])
        self.assertEqual(result.skills, [])
        self.assertIs(result.probe_result, probe_result)
        self.assertEqual(signal_bus.signals, [])

    async def test_pipeline_runs_retrieve_finalize_and_signal(self) -> None:
        """Recall pipeline preserves helper calls, runtime finalization, and signal."""
        signal_bus = _SignalBus()
        typed_query = TypedQuery(
            query="auth preference",
            context_type=ContextType.MEMORY,
            intent="memory",
            target_directories=[],
        )
        explain = SearchExplain(
            query_class="simple_recall",
            path="vector",
            rerank_ms=0.0,
        )
        query_result = QueryResult(
            query=typed_query,
            matched_contexts=[],
            searched_directories=[],
            timing_ms={
                "embed": 1.0,
                "search": 2.0,
                "rerank": 0.0,
                "assemble": 0.5,
                "total": 3.5,
            },
            explain=explain,
        )
        memory = MatchedContext(
            uri="opencortex://tenant/user/memories/preferences/auth",
            context_type=ContextType.MEMORY,
            is_leaf=True,
            abstract="User prefers JWT",
            score=0.9,
        )
        directory = MatchedContext(
            uri="opencortex://tenant/user/memories/preferences",
            context_type=ContextType.MEMORY,
            is_leaf=False,
            score=0.1,
        )
        runtime_result = MagicMock()
        orch = SimpleNamespace(
            _ensure_init=MagicMock(),
            _config=SimpleNamespace(explain_enabled=True),
            _memory_signal_bus=signal_bus,
            _memory_runtime=SimpleNamespace(
                finalize=MagicMock(return_value=runtime_result)
            ),
            bind_memory_runtime=MagicMock(
                return_value={"memory_limit": 4, "effective_depth": "l0"}
            ),
            _build_search_filter=MagicMock(return_value={"op": "and", "conds": []}),
            _execute_object_query=AsyncMock(return_value=query_result),
            _aggregate_results=MagicMock(
                return_value=FindResult(
                    memories=[memory, directory],
                    resources=[],
                    skills=[],
                )
            ),
        )
        memory_service = SimpleNamespace(
            _orch=orch,
            _build_typed_queries=MagicMock(return_value=[typed_query]),
            _summarize_retrieve_breakdown=MagicMock(
                return_value={
                    "embed": 1.0,
                    "search": 2.0,
                    "rerank": 0.0,
                    "assemble": 0.5,
                    "total": 3.5,
                }
            ),
        )
        query_service = MemoryQueryService(memory_service)
        pipeline = MemoryRecallPipelineService(query_service)
        probe_result = SearchResult(should_recall=True)
        retrieve_plan = MagicMock(retrieval_depth=RetrievalDepth.L0)
        tokens = set_request_identity("tenant", "user")
        try:
            result = await pipeline.search(
                "auth preference",
                target_uri="opencortex://tenant/user/memories/preferences",
                probe_result=probe_result,
                retrieve_plan=retrieve_plan,
            )
        finally:
            reset_request_identity(tokens)

        memory_service._build_typed_queries.assert_called_once()
        memory_service._summarize_retrieve_breakdown.assert_called_once_with(
            [query_result]
        )
        orch._execute_object_query.assert_awaited_once()
        orch._memory_runtime.finalize.assert_called_once()
        self.assertEqual(result.memories, [memory])
        self.assertEqual(result.total, 1)
        self.assertIs(result.runtime_result, runtime_result)
        self.assertIsNotNone(result.explain_summary)
        self.assertEqual(
            typed_query.target_directories,
            ["opencortex://tenant/user/memories/preferences"],
        )
        self.assertEqual(len(signal_bus.signals), 1)
        signal = signal_bus.signals[0]
        self.assertIsInstance(signal, RecallCompletedSignal)
        self.assertEqual(signal.query, "auth preference")
        self.assertEqual(signal.tenant_id, "tenant")
        self.assertEqual(signal.user_id, "user")
        self.assertEqual(signal.memories, [memory])
