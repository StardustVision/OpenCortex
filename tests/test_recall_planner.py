import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.cognition import RecallPlanner
from opencortex.config import CortexConfig
from opencortex.cognition.state_types import OwnerType
from opencortex.http.request_context import (
    reset_request_identity,
    reset_request_project_id,
    set_request_identity,
    set_request_project_id,
)
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.types import (
    ContextType,
    DetailLevel,
    FindResult,
    MatchedContext,
    QueryResult,
    RecallPlan,
    RecallSurface,
    SearchIntent,
    TypedQuery,
)
from test_e2e_phase1 import InMemoryStorage, MockEmbedder


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

    def test_invalid_detail_override_falls_back_to_intent_detail(self):
        planner = RecallPlanner(cone_enabled=False)
        intent = SearchIntent(
            detail_level=DetailLevel.L1,
            should_recall=True,
        )

        plan = planner.plan(
            query="bad override",
            intent=intent,
            max_items=4,
            recall_mode="auto",
            include_knowledge=False,
            detail_level_override="l3",
        )

        self.assertEqual(plan.detail_level, DetailLevel.L1)


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
        self.assertEqual(intent.detail_level, DetailLevel.L2)
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


class TestOrchestratorAutophagyIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="opencortex_orch_autophagy_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
            query_classifier_enabled=False,
            hyde_enabled=False,
            explain_enabled=False,
            cone_retrieval_enabled=False,
        )
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()
        self.identity_tokens = set_request_identity("tenant-1", "user-1")
        self.orch = MemoryOrchestrator(
            config=CortexConfig(
                query_classifier_enabled=False,
                hyde_enabled=False,
                explain_enabled=False,
                cone_retrieval_enabled=True,
            )
        )

    def tearDown(self):
        reset_request_identity(self.identity_tokens)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_orchestrator(self) -> MemoryOrchestrator:
        return MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=self.embedder,
        )

    async def test_init_creates_autophagy_kernel(self):
        orch = self._make_orchestrator()

        await orch.init()

        self.assertIsNotNone(orch._cognitive_state_store)
        self.assertIsNotNone(orch._autophagy_kernel)

        await orch.close()

    async def test_add_initializes_memory_owner_state_with_persisted_record_id(self):
        orch = self._make_orchestrator()
        await orch.init()
        orch._autophagy_kernel.initialize_owner = AsyncMock()

        ctx = await orch.add(
            abstract="prefers dark mode",
            category="preferences",
            context_type="memory",
        )

        orch._autophagy_kernel.initialize_owner.assert_awaited_once_with(
            owner_type=OwnerType.MEMORY,
            owner_id=ctx.id,
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="public",
        )

        await orch.close()

    async def test_add_dedup_skipped_initializes_existing_memory_owner_state(self):
        orch = self._make_orchestrator()
        await orch.init()
        existing_uri = "opencortex://tenant-1/user-1/memories/events/existing-skip"
        await self.storage.upsert(
            "context",
            {
                "id": "existing-skip-id",
                "uri": existing_uri,
                "is_leaf": True,
                "abstract": "existing event",
                "overview": "",
                "context_type": "memory",
                "category": "events",
                "source_tenant_id": "tenant-1",
                "source_user_id": "user-1",
                "scope": "private",
                "project_id": "public",
            },
        )
        orch._autophagy_kernel.initialize_owner = AsyncMock()
        orch._check_duplicate = AsyncMock(return_value=(existing_uri, 0.93))

        ctx = await orch.add(
            abstract="existing event",
            category="events",
            context_type="memory",
            dedup=True,
        )

        self.assertEqual(ctx.meta["dedup_action"], "skipped")
        self.assertEqual(ctx.uri, existing_uri)
        orch._autophagy_kernel.initialize_owner.assert_awaited_once_with(
            owner_type=OwnerType.MEMORY,
            owner_id="existing-skip-id",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="public",
        )

        await orch.close()

    async def test_add_dedup_merged_initializes_existing_memory_owner_state(self):
        orch = self._make_orchestrator()
        await orch.init()
        existing_uri = "opencortex://tenant-1/user-1/memories/preferences/existing-merge"
        await self.storage.upsert(
            "context",
            {
                "id": "existing-merge-id",
                "uri": existing_uri,
                "is_leaf": True,
                "abstract": "dark mode",
                "overview": "",
                "context_type": "memory",
                "category": "preferences",
                "source_tenant_id": "tenant-1",
                "source_user_id": "user-1",
                "scope": "private",
                "project_id": "public",
            },
        )
        orch._autophagy_kernel.initialize_owner = AsyncMock()
        orch._check_duplicate = AsyncMock(return_value=(existing_uri, 0.96))
        orch._merge_into = AsyncMock()

        ctx = await orch.add(
            abstract="dark mode",
            category="preferences",
            context_type="memory",
            dedup=True,
        )

        self.assertEqual(ctx.meta["dedup_action"], "merged")
        self.assertEqual(ctx.uri, existing_uri)
        orch._merge_into.assert_awaited_once_with(existing_uri, "dark mode", "")
        orch._autophagy_kernel.initialize_owner.assert_awaited_once_with(
            owner_type=OwnerType.MEMORY,
            owner_id="existing-merge-id",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="public",
        )

        await orch.close()

    async def test_on_trace_saved_uses_trace_project_id_instead_of_ambient_project(self):
        orch = self._make_orchestrator()
        await orch.init()
        orch._autophagy_kernel.initialize_owner = AsyncMock()
        trace = Mock(
            trace_id="trace-proj-1",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-42",
        )
        public_project = set_request_project_id("public")
        try:
            await orch._on_trace_saved(trace)
        finally:
            reset_request_project_id(public_project)

        orch._autophagy_kernel.initialize_owner.assert_awaited_once_with(
            owner_type=OwnerType.TRACE,
            owner_id="trace-proj-1",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-42",
        )

        await orch.close()

    async def test_search_applies_autophagy_recall_outcome_for_recalled_memory_ids(self):
        orch = self._make_orchestrator()
        await orch.init()
        recalled_uri = "opencortex://tenant-1/user-1/memories/preferences/mem-123"
        await self.storage.upsert(
            "context",
            {
                "id": "mem-123",
                "uri": recalled_uri,
                "is_leaf": True,
                "abstract": "dark mode preference",
                "overview": "User prefers dark mode",
                "context_type": "memory",
                "category": "preferences",
                "source_tenant_id": "tenant-1",
                "source_user_id": "user-1",
                "scope": "private",
                "project_id": "public",
            },
        )
        orch.plan_recall = AsyncMock(
            return_value=(
                SearchIntent(
                    intent_type="quick_lookup",
                    detail_level=DetailLevel.L1,
                    should_recall=True,
                ),
                RecallPlan(
                    should_recall=True,
                    surfaces=[RecallSurface.MEMORY],
                    detail_level=DetailLevel.L1,
                    memory_limit=1,
                    knowledge_limit=0,
                    enable_cone=False,
                    fusion_policy="memory_only",
                    reasoning="test",
                ),
            )
        )
        orch._retriever = Mock()
        orch._retriever.retrieve = AsyncMock(
            return_value=QueryResult(
                query=TypedQuery(
                    query="hello",
                    context_type=ContextType.MEMORY,
                    intent="",
                    detail_level=DetailLevel.L1,
                ),
                matched_contexts=[
                    MatchedContext(
                        uri=recalled_uri,
                        context_type=ContextType.MEMORY,
                        is_leaf=True,
                        abstract="dark mode preference",
                        overview="User prefers dark mode",
                        category="preferences",
                        score=0.9,
                    )
                ],
                searched_directories=[],
            )
        )
        orch._autophagy_kernel.apply_recall_outcome = AsyncMock()

        result = await orch.search("hello", limit=1)

        self.assertEqual(len(result.memories), 1)
        orch._autophagy_kernel.apply_recall_outcome.assert_awaited_once_with(
            owner_ids=["mem-123"],
            query="hello",
            recall_outcome={"selected_results": ["mem-123"]},
        )

        await orch.close()

    async def test_search_ignores_autophagy_recall_failures(self):
        orch = self._make_orchestrator()
        await orch.init()
        recalled_uri = "opencortex://tenant-1/user-1/memories/preferences/mem-456"
        await self.storage.upsert(
            "context",
            {
                "id": "mem-456",
                "uri": recalled_uri,
                "is_leaf": True,
                "abstract": "dark mode preference",
                "overview": "User prefers dark mode",
                "context_type": "memory",
                "category": "preferences",
                "source_tenant_id": "tenant-1",
                "source_user_id": "user-1",
                "scope": "private",
                "project_id": "public",
            },
        )
        orch.plan_recall = AsyncMock(
            return_value=(
                SearchIntent(
                    intent_type="quick_lookup",
                    detail_level=DetailLevel.L1,
                    should_recall=True,
                ),
                RecallPlan(
                    should_recall=True,
                    surfaces=[RecallSurface.MEMORY],
                    detail_level=DetailLevel.L1,
                    memory_limit=1,
                    knowledge_limit=0,
                    enable_cone=False,
                    fusion_policy="memory_only",
                    reasoning="test",
                ),
            )
        )
        orch._retriever = Mock()
        orch._retriever.retrieve = AsyncMock(
            return_value=QueryResult(
                query=TypedQuery(
                    query="hello",
                    context_type=ContextType.MEMORY,
                    intent="",
                    detail_level=DetailLevel.L1,
                ),
                matched_contexts=[
                    MatchedContext(
                        uri=recalled_uri,
                        context_type=ContextType.MEMORY,
                        is_leaf=True,
                        abstract="dark mode preference",
                        overview="User prefers dark mode",
                        category="preferences",
                        score=0.9,
                    )
                ],
                searched_directories=[],
            )
        )
        orch._autophagy_kernel.apply_recall_outcome = AsyncMock(
            side_effect=RuntimeError("persist failure")
        )

        result = await orch.search("hello", limit=1)

        self.assertEqual([memory.uri for memory in result.memories], [recalled_uri])

        await orch.close()

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
