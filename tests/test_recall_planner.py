import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.retrieve.types import (
    DetailLevel,
    FindResult,
    RecallPlan,
    RecallSurface,
    SearchIntent,
)


class TestRecallPlanTypes(unittest.TestCase):
    def test_recall_plan_to_dict_serializes_surfaces(self):
        plan = RecallPlan(
            should_recall=True,
            surfaces=[RecallSurface.MEMORY, RecallSurface.KNOWLEDGE],
            detail_level=DetailLevel.L1,
            memory_limit=5,
            knowledge_limit=3,
            enable_cone=True,
            fusion_policy="memory_then_knowledge",
            reasoning="intent=recent_recall include_knowledge=true",
        )

        self.assertEqual(
            plan.to_dict(),
            {
                "should_recall": True,
                "surfaces": ["memory", "knowledge"],
                "detail_level": "l1",
                "memory_limit": 5,
                "knowledge_limit": 3,
                "enable_cone": True,
                "fusion_policy": "memory_then_knowledge",
                "reasoning": "intent=recent_recall include_knowledge=true",
            },
        )

    def test_find_result_to_dict_emits_recall_plan(self):
        intent = SearchIntent(intent_type="recent_recall", top_k=5)
        intent.recall_plan = RecallPlan(
            should_recall=True,
            surfaces=[RecallSurface.MEMORY],
            detail_level=DetailLevel.L1,
            memory_limit=5,
            knowledge_limit=0,
            enable_cone=True,
            fusion_policy="memory_only",
            reasoning="default memory-only plan",
        )

        payload = FindResult(memories=[], resources=[], skills=[], search_intent=intent).to_dict()

        self.assertEqual(payload["recall_plan"]["surfaces"], ["memory"])
        self.assertEqual(payload["recall_plan"]["fusion_policy"], "memory_only")

    def test_find_result_to_dict_prefers_recall_plan_for_shared_fields(self):
        intent = SearchIntent(
            intent_type="recent_recall",
            top_k=5,
            detail_level=DetailLevel.L0,
            should_recall=False,
        )
        intent.recall_plan = RecallPlan(
            should_recall=True,
            surfaces=[RecallSurface.MEMORY],
            detail_level=DetailLevel.L2,
            memory_limit=5,
            knowledge_limit=0,
            enable_cone=True,
            fusion_policy="memory_only",
            reasoning="planner wins",
        )

        payload = FindResult(memories=[], resources=[], skills=[], search_intent=intent).to_dict()

        self.assertEqual(payload["search_intent"]["detail_level"], "l2")
        self.assertTrue(payload["search_intent"]["should_recall"])

    def test_find_result_to_dict_keeps_search_intent_fields_without_recall_plan(self):
        intent = SearchIntent(
            intent_type="recent_recall",
            top_k=5,
            detail_level=DetailLevel.L0,
            should_recall=False,
        )

        payload = FindResult(memories=[], resources=[], skills=[], search_intent=intent).to_dict()

        self.assertEqual(payload["search_intent"]["detail_level"], "l0")
        self.assertFalse(payload["search_intent"]["should_recall"])
        self.assertNotIn("recall_plan", payload)
