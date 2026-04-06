import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition import RecallPlanner
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


class TestRecallPlannerBehavior(unittest.TestCase):
    def test_plan_auto_mode_includes_knowledge_when_enabled(self):
        planner = RecallPlanner(cone_enabled=True)
        intent = SearchIntent(
            intent_type="recent_recall",
            top_k=7,
            detail_level=DetailLevel.L1,
            should_recall=True,
        )

        plan = planner.plan(
            query="最近怎么修这个问题？",
            intent=intent,
            max_items=5,
            recall_mode="auto",
            include_knowledge=True,
            detail_level_override=None,
        )

        self.assertTrue(plan.should_recall)
        self.assertEqual(
            plan.surfaces, [RecallSurface.MEMORY, RecallSurface.KNOWLEDGE]
        )
        self.assertEqual(plan.memory_limit, 7)
        self.assertEqual(plan.knowledge_limit, 3)
        self.assertTrue(plan.enable_cone)

    def test_plan_never_mode_turns_everything_off(self):
        planner = RecallPlanner(cone_enabled=True)
        intent = SearchIntent(
            should_recall=True,
            top_k=10,
            detail_level=DetailLevel.L2,
        )

        plan = planner.plan(
            query="anything",
            intent=intent,
            max_items=8,
            recall_mode="never",
            include_knowledge=True,
            detail_level_override=None,
        )

        self.assertFalse(plan.should_recall)
        self.assertEqual(plan.surfaces, [])
        self.assertEqual(plan.memory_limit, 0)
        self.assertEqual(plan.knowledge_limit, 0)
        self.assertFalse(plan.enable_cone)

    def test_plan_always_mode_forces_recall(self):
        planner = RecallPlanner(cone_enabled=False)
        intent = SearchIntent(
            should_recall=False,
            top_k=2,
            detail_level=DetailLevel.L1,
        )

        plan = planner.plan(
            query="force it",
            intent=intent,
            max_items=4,
            recall_mode="always",
            include_knowledge=False,
            detail_level_override=None,
        )

        self.assertTrue(plan.should_recall)
        self.assertEqual(plan.surfaces, [RecallSurface.MEMORY])
        self.assertEqual(plan.memory_limit, 4)

    def test_detail_override_wins_over_intent_default(self):
        planner = RecallPlanner(cone_enabled=False)
        intent = SearchIntent(
            detail_level=DetailLevel.L0,
            should_recall=True,
        )

        plan = planner.plan(
            query="show me details",
            intent=intent,
            max_items=4,
            recall_mode="auto",
            include_knowledge=False,
            detail_level_override="l2",
        )

        self.assertEqual(plan.detail_level, DetailLevel.L2)
