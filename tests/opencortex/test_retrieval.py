# SPDX-License-Identifier: Apache-2.0
"""Focused tests for retrieval planning and ranking internals."""

from __future__ import annotations

import asyncio
import unittest

from opencortex.core.identity import IdentityProfile
from opencortex.vector.retrieval.executor import RetrievalExecutor
from opencortex.vector.retrieval.planner import RetrievalPlanner
from opencortex.vector.retrieval.probe import query_type_for
from opencortex.vector.retrieval.ranker import RetrievalRanker
from opencortex.vector.retrieval.reranker import rerank_text
from opencortex.vector.retrieval.schemas import (
    ProbeEvidence,
    QueryType,
    RetrievalHit,
    RetrievalPlan,
    RetrievalProbeResult,
    RetrievalRequest,
    RetrievalSurface,
)


class TestRetrievalPlanning(unittest.TestCase):
    """Query type controls retrieval strategy without changing public schemas."""

    def test_factual_query_weights_fact_and_object_surfaces(self) -> None:
        """Factual plans prefer direct/fact surfaces."""
        planner = RetrievalPlanner()
        probe = RetrievalProbeResult(
            query_type=QueryType.FACTUAL,
            search_vectors=[[0.1, 0.2]],
            evidence=ProbeEvidence(
                top_score=0.9,
                object_top_score=0.9,
                candidate_count=1,
                object_candidate_count=1,
            ),
        )

        plan = planner.plan(RetrievalRequest(query="Alice Python"), probe=probe)

        self.assertGreater(
            plan.surface_weights[RetrievalSurface.FACT_INDEX],
            planner.base_surface_weights[RetrievalSurface.FACT_INDEX],
        )
        self.assertEqual(plan.query_type, QueryType.FACTUAL)
        self.assertFalse(plan.temporal.enabled)

    def test_temporal_query_builds_temporal_plan(self) -> None:
        """Explicit dates or order words become temporal plans."""
        self.assertEqual(
            query_type_for("latest Alice update after 2024-03"), QueryType.TEMPORAL
        )
        planner = RetrievalPlanner()
        probe = RetrievalProbeResult(
            query_type=QueryType.TEMPORAL,
            search_vectors=[[0.1, 0.2]],
            evidence=ProbeEvidence(candidate_count=1, locator_candidate_count=1),
        )

        plan = planner.plan(
            RetrievalRequest(query="latest Alice update after 2024-03"),
            probe=probe,
        )

        self.assertTrue(plan.temporal.enabled)
        self.assertEqual(plan.temporal.order, "latest")
        self.assertEqual(plan.temporal.after, "2024-03-01T00:00:00+00:00")


class TestRetrievalExecution(unittest.IsolatedAsyncioTestCase):
    """Executor handles surface failures independently."""

    async def test_surface_timeout_drops_only_that_surface(self) -> None:
        """One timed-out surface does not fail the whole retrieval."""
        store = _SlowVectorStore()
        executor = RetrievalExecutor(
            vector_store=store,
            collection_resolver=lambda: "context",
            surface_timeout_seconds=0.01,
        )
        plan = RetrievalPlan(
            query="Alice",
            limit=3,
            candidate_limit=3,
            surfaces=[RetrievalSurface.L0_OBJECT],
            search_vectors=[[0.1]],
        )

        hits = await executor.execute(
            plan=plan,
            profile=IdentityProfile(tenant_id="tenant", user_id="user"),
        )

        self.assertEqual(hits, [])


class TestRetrievalRanking(unittest.TestCase):
    """Ranker uses normalized scores and temporal tie-breaks."""

    def test_latest_temporal_tiebreak_uses_event_time(self) -> None:
        """Latest queries can sort lower-scored newer facts first."""
        plan = RetrievalPlan(
            query="latest Alice",
            query_type=QueryType.TEMPORAL,
            limit=2,
            candidate_limit=2,
            temporal={"enabled": True, "order": "latest"},
        )
        hits = [
            RetrievalHit(
                record={"uri": "old", "event_ts": "2024-01-01T00:00:00+00:00"},
                score=0.99,
                surface=RetrievalSurface.L0_OBJECT,
                source_uri="old",
            ),
            RetrievalHit(
                record={"uri": "new", "event_ts": "2024-05-01T00:00:00+00:00"},
                score=0.8,
                surface=RetrievalSurface.L0_OBJECT,
                source_uri="new",
            ),
        ]

        ranked = RetrievalRanker().rank(hits, plan=plan)

        self.assertEqual([hit.source_uri for hit in ranked], ["new", "old"])

    def test_rerank_text_includes_fact_points(self) -> None:
        """Rerank documents include fact-level details."""
        text = rerank_text(
            RetrievalHit(
                record={
                    "uri": "memory-1",
                    "abstract_json": {
                        "fact_points": [
                            "Alice visited Tokyo in 2024-03 with 2 teammates."
                        ]
                    },
                },
                score=1.0,
                surface=RetrievalSurface.FACT_INDEX,
                source_uri="memory-1",
            )
        )

        self.assertIn("2024-03", text)
        self.assertIn("2 teammates", text)


class _SlowVectorStore:
    """Vector store that always exceeds the executor surface timeout."""

    async def search(
        self, *_args: object, **_kwargs: object
    ) -> list[dict[str, object]]:
        """Sleep longer than the configured timeout."""
        await asyncio.sleep(0.2)
        return [{"uri": "late", "_score": 1.0}]
