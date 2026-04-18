import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.intent import (
    QueryRewriteMode,
    RecallPlanner,
    RetrievalDepth,
    SearchResult,
)
from opencortex.memory import MemoryKind


class TestIntentPlannerPhase2(unittest.TestCase):
    def test_lookup_query_produces_narrow_plan(self):
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="Which file did we edit for the retry fix?",
            probe_result=SearchResult(
                should_recall=True,
                candidate_entries=[
                    {"uri": "opencortex://memory/events/1", "memory_kind": "event"},
                ],
                evidence={
                    "top_score": 0.92,
                    "score_gap": 0.2,
                    "candidate_count": 1,
                    "object_candidate_count": 1,
                },
            ),
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.target_memory_kinds[0], MemoryKind.EVENT)
        self.assertEqual(plan.retrieval_depth, RetrievalDepth.L0)
        self.assertEqual(plan.decision, "stop_l0")
        self.assertLess(plan.search_profile.association_budget, 0.3)

    def test_profile_query_prioritizes_profile_surfaces(self):
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="Based on my habits, what would I likely pick?",
            probe_result=SearchResult(
                should_recall=True,
                candidate_entries=[
                    {
                        "uri": "opencortex://memory/profile/1",
                        "memory_kind": "profile",
                    }
                ],
                evidence={
                    "top_score": 0.7,
                    "score_gap": 0.1,
                    "candidate_count": 1,
                },
            ),
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )

        assert plan is not None
        self.assertEqual(plan.target_memory_kinds[:3], [
            MemoryKind.PROFILE,
            MemoryKind.PREFERENCE,
            MemoryKind.CONSTRAINT,
        ])
        self.assertEqual(plan.retrieval_depth, RetrievalDepth.L2)
        self.assertEqual(plan.decision, "hydrate_l2")
        self.assertTrue(plan.search_profile.rerank)

    def test_relational_query_increases_association_budget(self):
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="What happened before the launch review?",
            probe_result=SearchResult(
                should_recall=True,
                candidate_entries=[
                    {
                        "uri": "opencortex://memory/relation/1",
                        "memory_kind": "relation",
                    }
                ],
                query_entities=["launch review"],
                evidence={
                    "top_score": 0.6,
                    "score_gap": 0.06,
                    "candidate_count": 2,
                },
            ),
            max_items=6,
            recall_mode="auto",
            detail_level_override=None,
        )

        assert plan is not None
        self.assertEqual(plan.target_memory_kinds[0], MemoryKind.RELATION)
        self.assertGreater(plan.search_profile.association_budget, 0.0)
        self.assertEqual(plan.query_plan.rewrite_mode, QueryRewriteMode.NONE)
        self.assertEqual(plan.decision, "hydrate_l2")

    def test_full_content_request_can_request_l2(self):
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="Show me the full original content of the plan discussion.",
            probe_result=SearchResult(
                should_recall=True,
                candidate_entries=[
                    {
                        "uri": "opencortex://memory/docs/1",
                        "memory_kind": "document_chunk",
                    }
                ],
                evidence={
                    "top_score": 0.8,
                    "score_gap": 0.1,
                    "candidate_count": 2,
                },
            ),
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )

        assert plan is not None
        self.assertEqual(plan.retrieval_depth, RetrievalDepth.L2)
        self.assertEqual(plan.decision, "hydrate_l2")
        self.assertIn(MemoryKind.DOCUMENT_CHUNK, plan.target_memory_kinds)

    def test_lookup_query_keeps_cone_off_by_default(self):
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="Where do I live now in Hangzhou?",
            probe_result=SearchResult(
                should_recall=True,
                query_entities=["Hangzhou"],
                candidate_entries=[
                    {
                        "uri": "opencortex://memory/events/1",
                        "memory_kind": "event",
                    }
                ],
                evidence={
                    "top_score": 0.84,
                    "score_gap": 0.12,
                    "candidate_count": 1,
                },
            ),
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )

        assert plan is not None
        self.assertEqual(plan.target_memory_kinds[0], MemoryKind.EVENT)
        self.assertEqual(plan.search_profile.association_budget, 0.0)


if __name__ == "__main__":
    unittest.main()
