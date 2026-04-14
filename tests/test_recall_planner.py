import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from opencortex.config import CortexConfig, init_config
from opencortex.intent import ExecutionResult, RetrievalPlan, SearchResult
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
                "fallback": [],
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


if __name__ == "__main__":
    unittest.main()
