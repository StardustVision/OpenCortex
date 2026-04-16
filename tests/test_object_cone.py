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
from opencortex.retrieve.cone_scorer import ConeScorer
from opencortex.retrieve.entity_index import EntityIndex
from opencortex.retrieve.types import ContextType, DetailLevel, TypedQuery
from test_e2e_phase1 import InMemoryStorage, MockEmbedder


class TestObjectCone(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="opencortex_cone_")
        self.config = CortexConfig(
            data_root=self.temp_dir.name,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
            cone_retrieval_enabled=True,
            cone_weight=0.2,
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

    async def test_execute_object_query_expands_cone_candidates(self):
        collection = self.orch._get_collection()
        index = EntityIndex()
        index.add(collection, "anchor-id", ["redis"])
        index.add(collection, "related-id", ["redis"])
        index._built.add(collection)
        self.orch._entity_index = index
        self.orch._cone_scorer = ConeScorer(index)

        anchor_record = {
            "id": "anchor-id",
            "uri": "opencortex://memory/anchor",
            "_score": 0.92,
            "memory_kind": "event",
            "context_type": "memory",
            "category": "events",
            "is_leaf": True,
            "abstract": "Redis migration incident.",
            "anchor_hits": ["redis"],
            "abstract_json": {
                "anchors": [{"anchor_type": "entity", "value": "redis", "text": "redis"}]
            },
        }
        noise_record = {
            "id": "noise-id",
            "uri": "opencortex://memory/noise",
            "_score": 0.81,
            "memory_kind": "event",
            "context_type": "memory",
            "category": "events",
            "is_leaf": True,
            "abstract": "Frontend polish task.",
            "anchor_hits": ["frontend"],
            "abstract_json": {
                "anchors": [{"anchor_type": "topic", "value": "frontend", "text": "frontend"}]
            },
        }
        related_record = {
            "id": "related-id",
            "uri": "opencortex://memory/related",
            "memory_kind": "event",
            "context_type": "memory",
            "category": "events",
            "is_leaf": True,
            "abstract": "Redis cluster rollback details.",
            "anchor_hits": ["redis", "cluster"],
            "abstract_json": {
                "anchors": [
                    {"anchor_type": "entity", "value": "redis", "text": "redis"},
                    {"anchor_type": "topic", "value": "cluster", "text": "cluster"},
                ]
            },
        }

        self.storage.search = AsyncMock(return_value=[anchor_record, noise_record])
        self.storage.get = AsyncMock(return_value=[related_record])
        self.orch._embed_retrieval_query = AsyncMock(return_value=[0.1, 0.2, 0.3, 0.4])

        typed_query = TypedQuery(
            query="redis 集群发生了什么？",
            context_type=ContextType.MEMORY,
            intent="relational",
            detail_level=DetailLevel.L0,
        )
        probe_result = SearchResult(
            should_recall=True,
            anchor_hits=["redis"],
            candidate_entries=[
                {"uri": "opencortex://memory/anchor", "memory_kind": "event"},
                {"uri": "opencortex://memory/noise", "memory_kind": "event"},
            ],
            evidence={"candidate_count": 2, "top_score": 0.9, "score_gap": 0.1},
        )
        retrieve_plan = RetrievalPlan(
            target_memory_kinds=["event", "relation"],
            query_plan={
                "anchors": [{"kind": "entity", "value": "redis"}],
                "rewrite_mode": "none",
            },
            search_profile={
                "recall_budget": 0.5,
                "association_budget": 0.75,
                "rerank": True,
            },
            retrieval_depth="l0",
        )

        result = await self.orch._execute_object_query(
            typed_query=typed_query,
            limit=3,
            score_threshold=None,
            search_filter=None,
            retrieve_plan=retrieve_plan,
            probe_result=probe_result,
        )

        uris = [context.uri for context in result.matched_contexts]
        self.assertIn("opencortex://memory/related", uris)
        self.assertGreater(result.explain.frontier_waves, 0)
        self.storage.get.assert_awaited()


if __name__ == "__main__":
    unittest.main()
