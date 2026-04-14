import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from opencortex.config import CortexConfig, init_config
from opencortex.intent import RetrievalPlan, SearchResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.types import ContextType, DetailLevel, TypedQuery
from test_e2e_phase1 import InMemoryStorage, MockEmbedder


class TestObjectRerank(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="opencortex_rerank_")
        self.config = CortexConfig(
            data_root=self.temp_dir.name,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        )
        init_config(self.config)
        self.storage = InMemoryStorage()
        self.orch = MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=MockEmbedder(),
        )
        await self.orch.init()

    async def asyncTearDown(self):
        await self.orch.close()
        self.temp_dir.cleanup()

    async def test_execute_object_query_promotes_structured_anchor_match(self):
        right_record = {
            "id": "right-id",
            "uri": "opencortex://memory/right",
            "_score": 0.72,
            "memory_kind": "event",
            "context_type": "memory",
            "category": "events",
            "is_leaf": True,
            "abstract": "下周二去杭州出差，住在西湖边。",
            "anchor_hits": ["杭州", "下周二", "西湖"],
            "abstract_json": {
                "anchors": [
                    {"anchor_type": "entity", "value": "杭州", "text": "杭州"},
                    {"anchor_type": "time", "value": "下周二", "text": "下周二"},
                    {"anchor_type": "topic", "value": "西湖", "text": "西湖"},
                ]
            },
        }
        wrong_record = {
            "id": "wrong-id",
            "uri": "opencortex://memory/wrong",
            "_score": 0.84,
            "memory_kind": "event",
            "context_type": "memory",
            "category": "events",
            "is_leaf": True,
            "abstract": "周末去上海开会。",
            "anchor_hits": ["上海", "周末"],
            "abstract_json": {
                "anchors": [
                    {"anchor_type": "entity", "value": "上海", "text": "上海"},
                    {"anchor_type": "time", "value": "周末", "text": "周末"},
                ]
            },
        }

        self.storage.search = AsyncMock(return_value=[wrong_record, right_record])
        self.orch._embed_retrieval_query = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])

        typed_query = TypedQuery(
            query="下周二在杭州住哪里？",
            context_type=ContextType.MEMORY,
            intent="lookup",
            detail_level=DetailLevel.L0,
        )
        probe_result = SearchResult(
            should_recall=True,
            anchor_hits=["杭州", "下周二", "西湖"],
            candidate_entries=[
                {"uri": "opencortex://memory/right", "memory_kind": "event"},
                {"uri": "opencortex://memory/wrong", "memory_kind": "event"},
            ],
            evidence={"candidate_count": 2, "top_score": 0.83, "score_gap": 0.04},
        )
        retrieve_plan = RetrievalPlan(
            target_memory_kinds=["event", "summary"],
            query_plan={
                "anchors": [
                    {"kind": "entity", "value": "杭州"},
                    {"kind": "time", "value": "下周二"},
                    {"kind": "topic", "value": "西湖"},
                ],
                "rewrite_mode": "light",
            },
            search_profile={
                "recall_budget": 0.5,
                "association_budget": 0.0,
                "rerank": True,
            },
            retrieval_depth="l0",
        )

        result = await self.orch._execute_object_query(
            typed_query=typed_query,
            limit=1,
            score_threshold=None,
            search_filter=None,
            retrieve_plan=retrieve_plan,
            probe_result=probe_result,
        )

        self.assertEqual(result.matched_contexts[0].uri, "opencortex://memory/right")
        self.assertGreater(result.timing_ms["rerank"], 0.0)
        self.assertEqual(self.storage.search.await_args.kwargs["limit"], 19)


if __name__ == "__main__":
    unittest.main()
