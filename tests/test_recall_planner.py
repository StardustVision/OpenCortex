import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition import RecallPlanner
from opencortex.config import CortexConfig
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    FindResult,
    QueryResult,
    RecallPlan,
    RecallSurface,
    SearchIntent,
    TypedQuery,
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


class TestOrchestratorRecallPlanning(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = CortexConfig(
            query_classifier_enabled=False,
            hyde_enabled=False,
            explain_enabled=False,
            cone_retrieval_enabled=True,
        )
        self.orch = MemoryOrchestrator(config=self.config)

    async def test_plan_recall_attaches_plan_to_intent(self):
        intent = SearchIntent(
            intent_type="recent_recall",
            top_k=6,
            detail_level=DetailLevel.L1,
            should_recall=True,
        )
        plan = RecallPlan(
            should_recall=False,
            surfaces=[],
            detail_level=DetailLevel.L2,
            memory_limit=0,
            knowledge_limit=0,
            enable_cone=False,
            fusion_policy="none",
            reasoning="planner override",
        )
        planner = Mock(return_value=plan)
        planner.plan = Mock(return_value=plan)
        self.orch._recall_planner = planner

        returned_intent, returned_plan = await self.orch.plan_recall(
            query="what happened",
            max_items=4,
            recall_mode="auto",
            include_knowledge=True,
            detail_level_override="l2",
            context_type=ContextType.MEMORY,
            session_context={"session_id": "s1"},
            search_intent=intent,
        )

        self.assertIs(returned_intent, intent)
        self.assertIs(returned_plan, plan)
        self.assertFalse(intent.should_recall)
        self.assertIs(intent.recall_plan, plan)
        planner.plan.assert_called_once_with(
            query="what happened",
            intent=intent,
            max_items=4,
            recall_mode="auto",
            include_knowledge=True,
            detail_level_override="l2",
        )

    async def test_plan_recall_never_mode_skips_llm_routing(self):
        with patch("opencortex.orchestrator.IntentRouter") as router_cls:
            intent, plan = await self.orch.plan_recall(
                query="hello",
                max_items=3,
                recall_mode="never",
                include_knowledge=True,
                detail_level_override=None,
                context_type=ContextType.MEMORY,
                session_context={"session_id": "s1"},
                search_intent=None,
            )

        router_cls.assert_not_called()
        self.assertFalse(intent.should_recall)
        self.assertEqual(intent.detail_level, DetailLevel.L1)
        self.assertIs(intent.recall_plan, plan)
        self.assertFalse(plan.should_recall)
        self.assertEqual(plan.surfaces, [])
        self.assertEqual(plan.detail_level, DetailLevel.L1)

    async def test_search_returns_empty_result_when_plan_disables_recall(self):
        self.orch._initialized = True
        self.orch._retriever = Mock()
        self.orch.plan_recall = AsyncMock(
            return_value=(
                SearchIntent(
                    intent_type="quick_lookup",
                    top_k=3,
                    detail_level=DetailLevel.L0,
                    should_recall=False,
                ),
                RecallPlan(
                    should_recall=False,
                    surfaces=[],
                    detail_level=DetailLevel.L0,
                    memory_limit=0,
                    knowledge_limit=0,
                    enable_cone=False,
                    fusion_policy="none",
                    reasoning="disabled",
                ),
            )
        )

        result = await self.orch.search("hello")

        self.assertEqual(result.memories, [])
        self.assertEqual(result.resources, [])
        self.assertEqual(result.skills, [])
        self.assertFalse(result.search_intent.should_recall)
        self.orch.plan_recall.assert_awaited_once_with(
            query="hello",
            max_items=5,
            recall_mode="auto",
            include_knowledge=False,
            detail_level_override=None,
            context_type=None,
            session_context=None,
            search_intent=None,
        )
        self.orch._retriever.retrieve.assert_not_called()

    async def test_search_applies_recall_plan_detail_level_to_existing_queries(self):
        typed_query = TypedQuery(
            query="what happened",
            context_type=ContextType.MEMORY,
            intent="recent",
            detail_level=DetailLevel.L0,
        )
        intent = SearchIntent(
            intent_type="recent_recall",
            top_k=3,
            detail_level=DetailLevel.L0,
            should_recall=True,
            queries=[typed_query],
        )
        recall_plan = RecallPlan(
            should_recall=True,
            surfaces=[RecallSurface.MEMORY],
            detail_level=DetailLevel.L2,
            memory_limit=4,
            knowledge_limit=0,
            enable_cone=True,
            fusion_policy="memory_only",
            reasoning="upgrade detail",
        )

        self.orch._initialized = True
        self.orch._retriever = Mock()
        self.orch._retriever.retrieve = AsyncMock(
            return_value=QueryResult(
                query=typed_query,
                matched_contexts=[],
                searched_directories=[],
            )
        )
        self.orch._aggregate_results = Mock(
            return_value=FindResult(memories=[], resources=[], skills=[])
        )
        self.orch.plan_recall = AsyncMock(return_value=(intent, recall_plan))

        await self.orch.search(
            "what happened",
            search_intent=intent,
            detail_level="l1",
        )

        self.assertEqual(typed_query.detail_level, DetailLevel.L2)
        self.orch.plan_recall.assert_awaited_once_with(
            query="what happened",
            max_items=5,
            recall_mode="auto",
            include_knowledge=False,
            detail_level_override=None,
            context_type=None,
            session_context=None,
            search_intent=intent,
        )
        retrieve_args, retrieve_kwargs = self.orch._retriever.retrieve.await_args
        self.assertIs(retrieve_args[0], typed_query)
        self.assertEqual(retrieve_kwargs["limit"], 4)
