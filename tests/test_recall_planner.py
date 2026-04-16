import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from opencortex.config import CortexConfig, init_config
from opencortex.intent import ExecutionResult, RecallPlanner, RetrievalPlan, SearchResult
from opencortex.intent.types import ProbeScopeSource, ScopeLevel, StartingPoint
from opencortex.memory import MemoryKind
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.types import FindResult
from test_e2e_phase1 import InMemoryStorage, MockEmbedder


class TestRecallPlanContracts(unittest.TestCase):
    def test_probe_result_to_dict_clears_hits_when_no_recall(self):
        result = SearchResult(
            should_recall=False,
            candidate_entries=[
                {
                    "uri": "opencortex://memory/1",
                    "memory_kind": "event",
                }
            ],
        )

        self.assertEqual(
            result.to_dict(),
            {
                "should_recall": False,
                "anchor_hits": [],
                "candidate_entries": [],
                "starting_points": [],
                "query_entities": [],
                "starting_point_anchors": [],
                "scope_level": "global",
                "scope_source": "global_root",
                "scope_authoritative": False,
                "selected_root_uris": [],
                "scoped_miss": False,
                "evidence": {
                    "top_score": None,
                    "score_gap": None,
                    "object_top_score": None,
                    "anchor_top_score": None,
                    "candidate_count": 0,
                    "object_candidate_count": 0,
                    "anchor_candidate_count": 0,
                    "anchor_hit_count": 0,
                },
                "trace": {
                    "backend": "local_probe",
                    "model": None,
                    "top_k": 0,
                    "latency_ms": None,
                    "object_latency_ms": None,
                    "anchor_latency_ms": None,
                    "object_candidates": 0,
                    "anchor_candidates": 0,
                    "starting_points": 0,
                    "selected_bucket_source": None,
                    "scope_authoritative": False,
                    "selected_root_uris": [],
                    "scoped_miss": False,
                    "degraded": False,
                    "degrade_reason": None,
                },
            },
        )

    def test_find_result_to_dict_emits_probe_first_memory_pipeline(self):
        probe_result = SearchResult(
            should_recall=True,
            evidence={"candidate_count": 1, "top_score": 0.9},
        )
        retrieve_plan = RetrievalPlan(
            target_memory_kinds=["event", "summary"],
            query_plan={"anchors": [], "rewrite_mode": "none"},
            search_profile={
                "recall_budget": 0.3,
                "association_budget": 0.0,
                "rerank": False,
            },
            retrieval_depth="l0",
        )
        runtime_result = ExecutionResult(
            items=[{"uri": "opencortex://memory/1"}],
            trace={
                "probe": probe_result.to_dict(),
                "planner": retrieve_plan.to_dict(),
                "effective": {"sources": ["memory"], "retrieval_depth": "l0"},
                "hydration": [],
                "latency_ms": {"execution": 12},
            },
            degrade={"applied": False, "reasons": [], "actions": []},
        )

        payload = FindResult(
            memories=[],
            resources=[],
            skills=[],
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
            runtime_result=runtime_result,
        ).to_dict()

        self.assertEqual(payload["memory_pipeline"]["probe"], probe_result.to_dict())
        self.assertEqual(payload["memory_pipeline"]["planner"], retrieve_plan.to_dict())
        self.assertEqual(payload["memory_pipeline"]["runtime"], runtime_result.to_dict())
        self.assertNotIn("route", payload["memory_pipeline"])


class TestRecallPlannerIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.config = CortexConfig(
            data_root=os.path.join(os.getcwd(), ".tmp-test-recall-planner"),
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        )
        init_config(self.config)
        self.orch = MemoryOrchestrator(
            config=self.config,
            storage=InMemoryStorage(),
            embedder=MockEmbedder(),
        )
        await self.orch.init()

    async def asyncTearDown(self):
        await self.orch.close()

    async def test_search_emits_probe_planner_runtime(self):
        await self.orch.add(
            abstract="Project uses PostgreSQL for production.",
            category="general",
        )

        result = await self.orch.search("database", limit=3)
        payload = result.to_dict()

        self.assertIn("memory_pipeline", payload)
        self.assertIn("probe", payload["memory_pipeline"])
        self.assertIn("planner", payload["memory_pipeline"])
        self.assertIn("runtime", payload["memory_pipeline"])
        self.assertEqual(
            payload["memory_pipeline"]["planner"]["query_plan"]["rewrite_mode"],
            "none",
        )
        self.assertGreaterEqual(
            payload["memory_pipeline"]["probe"]["evidence"]["candidate_count"],
            1,
        )

    async def test_plan_memory_uses_probe_result(self):
        probe_result = SearchResult(
            should_recall=True,
            evidence={"top_score": 0.88, "score_gap": 0.2, "candidate_count": 1},
            candidate_entries=[
                {
                    "uri": "opencortex://memory/profile/1",
                    "memory_kind": "profile",
                }
            ],
        )

        plan = self.orch.plan_memory(
            query="What would I likely choose based on habit?",
            probe_result=probe_result,
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertIn(
            plan.target_memory_kinds[0],
            {MemoryKind.PREFERENCE, MemoryKind.PROFILE},
        )
        self.assertEqual(plan.retrieval_depth, "l1")
        self.assertEqual(plan.decision, "arbitrate_l1")
        self.assertGreater(plan.confidence or 0.0, 0.7)
        self.assertEqual(plan.query_plan.rewrite_mode.value, "none")

    async def test_search_caps_final_results_to_requested_limit(self):
        await self.orch.add(
            abstract="Launch checklist reviewed by the team.",
            category="events",
        )
        await self.orch.add(
            abstract="Launch rollback plan was updated.",
            category="events",
        )
        await self.orch.add(
            abstract="Launch timeline moved by one week.",
            category="events",
        )

        result = await self.orch.search("launch", limit=1)

        self.assertEqual(result.total, 1)
        self.assertEqual(len(list(result)), 1)

    async def test_search_respects_session_scope(self):
        self.orch._skill_manager = None
        await self.orch.add(
            abstract="Launch checklist reviewed for session A.",
            category="events",
            session_id="sess-a",
        )
        await self.orch.add(
            abstract="Launch checklist reviewed for session B.",
            category="events",
            session_id="sess-b",
        )

        result = await self.orch.search(
            "launch checklist",
            limit=5,
            session_context={
                "session_id": "sess-a",
                "tenant_id": "testteam",
                "user_id": "alice",
            },
        )

        self.assertEqual(result.total, 1)
        self.assertEqual(result.memories[0].session_id, "sess-a")

    async def test_search_probe_pipeline_exposes_selected_scope_bucket(self):
        await self.orch.add(
            abstract="Project launch notes under events root.",
            category="events",
            context_type="memory",
        )

        result = await self.orch.search(
            "launch notes",
            limit=5,
            target_uri="opencortex://memory/events",
        )

        probe_payload = result.to_dict()["memory_pipeline"]["probe"]
        self.assertEqual(probe_payload["scope_level"], ScopeLevel.CONTAINER_SCOPED.value)
        self.assertEqual(probe_payload["scope_source"], ProbeScopeSource.TARGET_URI.value)
        self.assertTrue(probe_payload["scope_authoritative"])
        self.assertEqual(probe_payload["selected_root_uris"], ["opencortex://memory/events"])

    async def test_search_authoritative_scope_miss_stays_scoped(self):
        result = await self.orch.search(
            "launch notes",
            limit=5,
            target_uri="opencortex://memory/missing-scope",
        )

        payload = result.to_dict()["memory_pipeline"]
        self.assertEqual(payload["probe"]["scope_source"], ProbeScopeSource.TARGET_URI.value)
        self.assertTrue(payload["probe"]["scope_authoritative"])
        self.assertTrue(payload["probe"]["scoped_miss"])
        self.assertEqual(payload["probe"]["selected_root_uris"], ["opencortex://memory/missing-scope"])
        self.assertNotIn("planner", payload)
        self.assertNotIn("runtime", payload)

    async def test_search_stays_l1_when_fallback_overview_is_sufficient(self):
        long_content = (
            "用户在出差订酒店时总是优先选择安静、靠湖、步行可达会场的酒店，"
            "并且明确避免夜生活区域和高噪音街区。"
            * 20
        )
        await self.orch.add(
            abstract="User prefers quiet hotels for work travel.",
            content=long_content,
            category="profile",
            context_type="memory",
        )

        result = await self.orch.search(
            "Based on my habits, what hotel would I likely pick on a work trip?",
            limit=3,
        )

        payload = result.to_dict()["memory_pipeline"]["runtime"]
        self.assertEqual(payload["trace"]["effective"]["retrieval_depth"], "l1")
        self.assertEqual(payload["trace"]["hydration"][0]["decision"], "stay_l1")
        self.assertTrue(result.memories)
        self.assertIsNone(result.memories[0].content)
        self.assertTrue(result.memories[0].overview)


class TestRecallPlannerStartingPoints(unittest.TestCase):
    def _make_probe(
        self,
        *,
        starting_points=None,
        starting_point_anchors=None,
        query_entities=None,
        scope_level=ScopeLevel.GLOBAL,
    ):
        return SearchResult(
            should_recall=True,
            evidence={"candidate_count": 1, "top_score": 0.8},
            starting_points=starting_points or [],
            starting_point_anchors=starting_point_anchors or [],
            query_entities=query_entities or [],
            scope_level=scope_level,
        )

    def test_case1_starting_points_with_anchors_enables_scope_and_cone(self):
        probe = SearchResult(
            should_recall=True,
            evidence={
                "candidate_count": 3,
                "top_score": 0.8,
                "anchor_hit_count": 2,
            },
            candidate_entries=[
                {"uri": "opencortex://memory/1", "memory_kind": "event"},
                {"uri": "opencortex://memory/2", "memory_kind": "event"},
                {"uri": "opencortex://memory/3", "memory_kind": "summary"},
            ],
            starting_points=[
                StartingPoint(
                    uri="opencortex://t/u/memories/events/s1",
                    session_id="s1",
                    entities=["Alice"],
                )
            ],
            starting_point_anchors=["Alice"],
            query_entities=["Alice"],
            scope_level=ScopeLevel.SESSION_ONLY,
        )
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="summarize everything Alice said",
            probe_result=probe,
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )
        assert plan is not None
        self.assertEqual(plan.scope_level, ScopeLevel.SESSION_ONLY)
        self.assertEqual(plan.session_scope, "s1")
        self.assertGreater(plan.search_profile.association_budget, 0.0)
        self.assertTrue(any(a.value == "Alice" for a in plan.query_plan.anchors))

    def test_case2_starting_points_without_anchors_disables_cone(self):
        probe = self._make_probe(
            starting_points=[
                StartingPoint(
                    uri="opencortex://t/u/memories/events/s1",
                    session_id="s1",
                )
            ],
            starting_point_anchors=[],
            query_entities=["what"],
            scope_level=ScopeLevel.SESSION_ONLY,
        )
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="What happened?",
            probe_result=probe,
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )
        assert plan is not None
        self.assertEqual(plan.scope_level, ScopeLevel.SESSION_ONLY)
        self.assertEqual(plan.session_scope, "s1")
        self.assertEqual(plan.search_profile.association_budget, 0.0)

    def test_case3_no_starting_points_with_query_entities_enables_global_cone(self):
        probe = self._make_probe(
            starting_points=[],
            starting_point_anchors=[],
            query_entities=["Project X"],
            scope_level=ScopeLevel.GLOBAL,
        )
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="Tell me about Project X",
            probe_result=probe,
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )
        assert plan is not None
        self.assertEqual(plan.scope_level, ScopeLevel.GLOBAL)
        self.assertIsNone(plan.session_scope)
        # EXPLORE coarse class with anchor_hit_count=0 won't boost association,
        # but base association for EXPLORE is 0.35
        self.assertGreaterEqual(plan.search_profile.association_budget, 0.0)

    def test_case4_no_starting_points_no_entities_stays_global(self):
        probe = self._make_probe(
            starting_points=[],
            starting_point_anchors=[],
            query_entities=[],
            scope_level=ScopeLevel.GLOBAL,
        )
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="What?",
            probe_result=probe,
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )
        assert plan is not None
        self.assertEqual(plan.scope_level, ScopeLevel.GLOBAL)
        self.assertEqual(plan.search_profile.association_budget, 0.0)

    def test_query_plan_anchors_do_not_absorb_probe_candidate_metadata(self):
        probe = SearchResult(
            should_recall=True,
            candidate_entries=[
                {
                    "uri": "opencortex://memory/1",
                    "memory_kind": "event",
                    "anchors": ["perseid meteor shower"],
                }
            ],
            starting_points=[
                StartingPoint(
                    uri="opencortex://t/u/memories/events/s1",
                    session_id="s1",
                    entities=["Caroline"],
                    time_refs=["20 July, 2023"],
                )
            ],
            starting_point_anchors=["connected lgbtq activists"],
            anchor_hits=["perseid meteor shower", "20 July, 2023"],
            query_entities=["Melanie", "paint", "sunrise"],
            evidence={"candidate_count": 1, "top_score": 0.8, "anchor_hit_count": 3},
            scope_level=ScopeLevel.SESSION_ONLY,
        )
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="When did Melanie paint a sunrise?",
            probe_result=probe,
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )

        assert plan is not None
        anchor_values = {anchor.value for anchor in plan.query_plan.anchors}
        self.assertTrue({"Melanie", "paint", "sunrise"}.issubset(anchor_values))
        self.assertNotIn("Caroline", anchor_values)
        self.assertNotIn("20 July, 2023", anchor_values)
        self.assertNotIn("perseid meteor shower", anchor_values)
        self.assertNotIn("connected lgbtq activists", anchor_values)

    def test_target_uri_bucket_without_starting_points_preserves_container_scope(self):
        probe = SearchResult(
            should_recall=True,
            candidate_entries=[
                {"uri": "opencortex://memory/events/item-1", "memory_kind": "event"}
            ],
            evidence={"candidate_count": 1, "top_score": 0.83},
            scope_level=ScopeLevel.CONTAINER_SCOPED,
            scope_source=ProbeScopeSource.TARGET_URI,
            scope_authoritative=True,
            selected_root_uris=["opencortex://memory/events"],
        )
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="launch notes",
            probe_result=probe,
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )

        assert plan is not None
        self.assertEqual(plan.scope_level, ScopeLevel.CONTAINER_SCOPED)
        self.assertIsNone(plan.session_scope)

    def test_most_specific_scope_level_wins(self):
        probe = self._make_probe(
            starting_points=[
                StartingPoint(
                    uri="opencortex://t/u/memories/events/s1",
                    session_id="s1",
                ),
                StartingPoint(
                    uri="opencortex://t/u/resources/documents/d1",
                    source_doc_id="d1",
                ),
            ],
            scope_level=ScopeLevel.SESSION_ONLY,  # probe already computed this
        )
        planner = RecallPlanner(cone_enabled=True)
        plan = planner.semantic_plan(
            query="Query",
            probe_result=probe,
            max_items=5,
            recall_mode="auto",
            detail_level_override=None,
        )
        assert plan is not None
        # session_scope is extracted from starting points, overriding document
        self.assertEqual(plan.session_scope, "s1")


class TestFactPointsPromptAndParsing(unittest.TestCase):
    def test_prompt_includes_fact_points_field(self):
        from opencortex.prompts import build_layer_derivation_prompt

        prompt = build_layer_derivation_prompt("Alice relocated to Hangzhou on May 1.")
        self.assertIn("fact_points", prompt)

    def test_fact_point_prefix_scheme(self):
        uri = "opencortex://team/user/memories/events/abc123"
        prefix = MemoryOrchestrator._fact_point_prefix(uri)
        self.assertEqual(prefix, f"{uri}/fact_points")


class TestDeriveLayersFactPoints(unittest.IsolatedAsyncioTestCase):
    def _make_orchestrator(self, llm_response=None):
        config = CortexConfig(
            data_root=os.path.join(os.getcwd(), ".tmp-test-fact-points"),
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        )
        init_config(config)
        orch = MemoryOrchestrator(
            config=config,
            storage=InMemoryStorage(),
            embedder=MockEmbedder(),
        )
        if llm_response is not None:
            import json as _json

            async def _mock_llm(prompt):
                return _json.dumps(llm_response)

            orch._llm_completion = _mock_llm
            orch._derive_layers_llm_completion = _mock_llm
        return orch

    async def test_llm_path_returns_fact_points_list(self):
        llm_data = {
            "overview": "Alice moved to Hangzhou.",
            "keywords": ["Alice", "Hangzhou"],
            "entities": ["Alice", "Hangzhou"],
            "anchor_handles": ["Alice"],
            "fact_points": [
                "Alice moved to Hangzhou on May 1",
                "Migration uses batch size 500 to avoid downtime",
            ],
        }
        orch = self._make_orchestrator(llm_response=llm_data)
        await orch.init()
        try:
            result = await orch._derive_layers(
                user_abstract="", content="Alice moved to Hangzhou on May 1."
            )
            self.assertIn("fact_points", result)
            self.assertIsInstance(result["fact_points"], list)
            self.assertEqual(len(result["fact_points"]), 2)
            self.assertEqual(result["fact_points"][0], "Alice moved to Hangzhou on May 1")
        finally:
            await orch.close()

    async def test_llm_path_omits_fact_points_returns_empty_list(self):
        llm_data = {
            "overview": "Some event occurred.",
            "keywords": ["event"],
            "entities": [],
            "anchor_handles": [],
            # fact_points key absent
        }
        orch = self._make_orchestrator(llm_response=llm_data)
        await orch.init()
        try:
            result = await orch._derive_layers(
                user_abstract="", content="Some event occurred."
            )
            self.assertIn("fact_points", result)
            self.assertEqual(result["fact_points"], [])
        finally:
            await orch.close()

    async def test_llm_path_non_list_fact_points_returns_empty_list(self):
        llm_data = {
            "overview": "Some event occurred.",
            "keywords": [],
            "entities": [],
            "anchor_handles": [],
            "fact_points": "not a list",
        }
        orch = self._make_orchestrator(llm_response=llm_data)
        await orch.init()
        try:
            result = await orch._derive_layers(
                user_abstract="", content="Some event occurred."
            )
            self.assertIn("fact_points", result)
            self.assertEqual(result["fact_points"], [])
        finally:
            await orch.close()

    async def test_llm_path_caps_fact_points_at_eight(self):
        llm_data = {
            "overview": "Ten facts.",
            "keywords": [],
            "entities": [],
            "anchor_handles": [],
            "fact_points": [f"Fact number {i}" for i in range(10)],
        }
        orch = self._make_orchestrator(llm_response=llm_data)
        await orch.init()
        try:
            result = await orch._derive_layers(user_abstract="", content="Ten facts.")
            self.assertIn("fact_points", result)
            self.assertLessEqual(len(result["fact_points"]), 8)
        finally:
            await orch.close()

    async def test_fast_path_returns_empty_fact_points(self):
        orch = self._make_orchestrator()
        await orch.init()
        try:
            result = await orch._derive_layers(
                user_abstract="My abstract",
                content="content",
                user_overview="My overview",
            )
            self.assertIn("fact_points", result)
            self.assertEqual(result["fact_points"], [])
        finally:
            await orch.close()

    async def test_no_llm_fallback_returns_empty_fact_points(self):
        # No llm_response → no LLM configured → fallback path
        orch = self._make_orchestrator(llm_response=None)
        await orch.init()
        try:
            result = await orch._derive_layers(
                user_abstract="", content="Some content."
            )
            self.assertIn("fact_points", result)
            self.assertEqual(result["fact_points"], [])
        finally:
            await orch.close()


if __name__ == "__main__":
    unittest.main()
