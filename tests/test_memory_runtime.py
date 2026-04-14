import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.intent import (
    MemoryExecutor,
    MemoryQueryPlan,
    MemorySearchProfile,
    RetrievalDepth,
    RetrievalPlan,
    SearchResult,
)
from opencortex.memory import MemoryKind


class TestMemoryRuntime(unittest.TestCase):
    def setUp(self):
        self.runtime = MemoryExecutor()
        self.probe = SearchResult(
            should_recall=True,
            candidate_entries=[
                {
                    "uri": "opencortex://memory/relation/1",
                    "memory_kind": "relation",
                }
            ],
            evidence={
                "top_score": 0.3,
                "score_gap": 0.02,
                "candidate_count": 2,
            },
        )
        self.plan = RetrievalPlan(
            target_memory_kinds=[
                MemoryKind.RELATION,
                MemoryKind.EVENT,
                MemoryKind.DOCUMENT_CHUNK,
            ],
            query_plan=MemoryQueryPlan(),
            search_profile=MemorySearchProfile(
                recall_budget=0.6,
                association_budget=0.65,
                rerank=True,
            ),
            retrieval_depth=RetrievalDepth.L1,
        )

    def test_bind_projects_memory_kinds_into_execution_hints(self):
        bound = self.runtime.bind(
            probe_result=self.probe,
            retrieve_plan=self.plan,
            max_items=5,
            session_id="sess-1",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
            include_knowledge=True,
        )

        self.assertEqual(bound["sources"], ["memory", "knowledge"])
        self.assertEqual(bound["scope"]["session_id"], "sess-1")
        self.assertEqual(bound["memory_limit"], 8)
        self.assertEqual(bound["knowledge_limit"], 3)
        self.assertEqual(bound["association_mode"], "normal")
        self.assertEqual(bound["raw_candidate_cap"], 25)
        self.assertEqual(bound["seed_uri_cap"], 10)
        self.assertEqual(bound["anchor_cap"], 6)
        self.assertFalse(bound["bind_start_points"])
        self.assertIn("memory", bound["context_types"])
        self.assertIn("resource", bound["context_types"])
        self.assertIn("relation", bound["category_filter"])
        self.assertIn("document_chunk", bound["category_filter"])

    def test_finalize_emits_machine_readable_trace_and_degrade(self):
        bound = self.runtime.bind(
            probe_result=self.probe,
            retrieve_plan=self.plan,
            max_items=5,
            session_id="sess-1",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
            include_knowledge=False,
        )

        result = self.runtime.finalize(
            bound_plan=bound,
            items=[{"uri": "opencortex://memory/1", "score": 0.9}],
            latency_ms=18,
        )

        self.assertEqual(result.items[0]["uri"], "opencortex://memory/1")
        self.assertEqual(result.trace.effective["sources"], ["memory"])
        self.assertEqual(result.trace.effective["retrieval_depth"], "l1")
        self.assertEqual(result.trace.effective["association_mode"], "normal")
        self.assertTrue(result.trace.effective["rerank"])
        self.assertEqual(result.trace.effective["raw_candidate_cap"], 25)
        self.assertEqual(
            result.trace.probe["candidate_entries"][0]["memory_kind"],
            "relation",
        )
        self.assertFalse(result.degrade.applied)

    def test_bind_can_anchor_start_points_without_seed_uris(self):
        anchor_only_probe = SearchResult(
            should_recall=True,
            anchor_hits=["杭州", "下周二"],
            evidence={
                "candidate_count": 1,
                "anchor_candidate_count": 1,
                "anchor_hit_count": 2,
                "anchor_top_score": 0.68,
            },
        )
        anchor_plan = RetrievalPlan(
            target_memory_kinds=[MemoryKind.EVENT, MemoryKind.SUMMARY],
            query_plan=MemoryQueryPlan(
                anchors=[
                    {"kind": "entity", "value": "杭州"},
                    {"kind": "time", "value": "下周二"},
                ]
            ),
            search_profile=MemorySearchProfile(
                recall_budget=0.45,
                association_budget=0.0,
                rerank=True,
            ),
            retrieval_depth=RetrievalDepth.L1,
        )

        bound = self.runtime.bind(
            probe_result=anchor_only_probe,
            retrieve_plan=anchor_plan,
            max_items=5,
            session_id="sess-1",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
            include_knowledge=False,
        )

        self.assertTrue(bound["bind_start_points"])
        self.assertEqual(bound["anchor_cap"], 6)

    def test_degrade_can_disable_association_before_other_actions(self):
        bound = self.runtime.bind(
            probe_result=self.probe,
            retrieve_plan=self.plan,
            max_items=5,
            session_id="sess-1",
            tenant_id="tenant-1",
            user_id="user-1",
            project_id="project-1",
            include_knowledge=False,
        )

        degraded = self.runtime.apply_degrade(
            bound_plan=bound,
            reasons=["latency_budget_exceeded"],
            actions=["disable_association"],
        )

        self.assertTrue(degraded["degrade"]["applied"])
        self.assertEqual(degraded["trace"]["effective"]["association_mode"], "off")
        self.assertEqual(degraded["trace"]["effective"]["retrieval_depth"], "l1")


if __name__ == "__main__":
    unittest.main()
