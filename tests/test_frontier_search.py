"""
Tests for frontier batching search optimization.

Uses real QdrantStorageAdapter (embedded) + deterministic MockEmbedder.
No mocks on storage or retriever internals.
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from collections import defaultdict
from typing import List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
from opencortex.retrieve.types import ContextType, DetailLevel, TypedQuery
from opencortex.storage.qdrant.adapter import QdrantStorageAdapter


class MockEmbedder(DenseEmbedderBase):
    """Deterministic hash-based embedder. Dimension=128."""
    DIMENSION = 128

    def __init__(self):
        super().__init__(model_name="mock-embedder-v1")

    def embed(self, text: str) -> EmbedResult:
        return EmbedResult(dense_vector=self._text_to_vector(text))

    def get_dimension(self) -> int:
        return self.DIMENSION

    @staticmethod
    def _text_to_vector(text: str) -> List[float]:
        raw = []
        for i in range(128):
            bits = hash(f"{text}_{i}") & 0xFFFF
            raw.append((bits & 0xFF) / 255.0 - 0.5)
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


class StorageSpy(QdrantStorageAdapter):
    """Thin wrapper that counts search calls on real Qdrant."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.call_counts = {"search": 0}

    async def search(self, *args, **kwargs):
        self.call_counts["search"] += 1
        return await super().search(*args, **kwargs)

    def reset_counts(self):
        self.call_counts = {"search": 0}


class TestShouldRerankScoreKey(unittest.TestCase):
    """Test _should_rerank with score_key parameter."""

    def test_should_rerank_score_key_final_score(self):
        """score_key='_final_score' reads _final_score field."""
        retriever = HierarchicalRetriever(
            storage=None, embedder=None, rerank_config=None
        )
        # Gap > threshold (0.15) → should NOT rerank
        results = [{"_final_score": 0.9}, {"_final_score": 0.5}]
        self.assertFalse(retriever._should_rerank(results, score_key="_final_score"))

        # Gap <= threshold → should rerank
        results = [{"_final_score": 0.9}, {"_final_score": 0.85}]
        self.assertTrue(retriever._should_rerank(results, score_key="_final_score"))

    def test_should_rerank_default_score_key(self):
        """Default score_key='_score' preserves backward compat."""
        retriever = HierarchicalRetriever(
            storage=None, embedder=None, rerank_config=None
        )
        results = [{"_score": 0.9}, {"_score": 0.5}]
        self.assertFalse(retriever._should_rerank(results))

        results = [{"_score": 0.9}, {"_score": 0.85}]
        self.assertTrue(retriever._should_rerank(results))

    def test_should_rerank_single_result(self):
        """Single result → always False."""
        retriever = HierarchicalRetriever(
            storage=None, embedder=None, rerank_config=None
        )
        self.assertFalse(retriever._should_rerank([{"_score": 0.9}]))


class TestDiverseTruncate(unittest.TestCase):
    """Test _diverse_truncate frontier balancing."""

    def test_balances_three_branches(self):
        """3 root branches x 40 items, truncated to 64 → each branch >= 20."""
        frontier = []
        for branch in ["opencortex://t/shared/memories",
                        "opencortex://t/shared/resources",
                        "opencortex://t/shared/skills"]:
            for i in range(40):
                frontier.append((f"{branch}/node_{i}", 0.5 + i * 0.01))

        result = HierarchicalRetriever._diverse_truncate(frontier, 64)
        self.assertEqual(len(result), 64)

        # Count per branch
        counts = defaultdict(int)
        for uri, _ in result:
            root = "/".join(uri.split("/")[:5])
            counts[root] += 1

        for branch_count in counts.values():
            self.assertGreaterEqual(branch_count, 20)

    def test_no_truncation_when_under_limit(self):
        """Frontier under limit → returned unchanged."""
        frontier = [("uri_1", 0.5), ("uri_2", 0.3)]
        result = HierarchicalRetriever._diverse_truncate(frontier, 64)
        self.assertEqual(len(result), 2)

    def test_single_branch_gets_all(self):
        """Single branch → gets all slots."""
        frontier = [(f"opencortex://t/shared/mem/node_{i}", 0.1 * i) for i in range(100)]
        result = HierarchicalRetriever._diverse_truncate(frontier, 64)
        self.assertEqual(len(result), 64)


class TestFairSelect(unittest.TestCase):
    """Test _per_parent_fair_select quota enforcement."""

    def test_protects_cold_parents(self):
        """Hot parent with 20 children, cold parent with 2 → cold gets all 2."""
        children_by_parent = {
            "hot_dir": [{"uri": f"hot_{i}", "_final_score": 0.9 - i * 0.01}
                        for i in range(20)],
            "cold_dir": [{"uri": f"cold_{i}", "_final_score": 0.3 + i * 0.01}
                         for i in range(2)],
        }
        selected = HierarchicalRetriever._per_parent_fair_select(
            children_by_parent, min_quota=2, total_budget=10
        )
        cold_uris = {s["uri"] for s in selected if s["uri"].startswith("cold_")}
        self.assertEqual(cold_uris, {"cold_0", "cold_1"})

    def test_budget_cap_respected(self):
        """Total selected does not exceed budget."""
        children_by_parent = {
            f"dir_{i}": [{"uri": f"dir_{i}_child_{j}", "_final_score": 0.5}
                         for j in range(10)]
            for i in range(5)
        }
        selected = HierarchicalRetriever._per_parent_fair_select(
            children_by_parent, min_quota=2, total_budget=15
        )
        self.assertLessEqual(len(selected), 15)

    def test_min_quota_with_fewer_children(self):
        """Parent with fewer children than min_quota → takes all it has."""
        children_by_parent = {
            "dir_a": [{"uri": "a_0", "_final_score": 0.9}],  # only 1 child
            "dir_b": [{"uri": f"b_{i}", "_final_score": 0.5} for i in range(10)],
        }
        selected = HierarchicalRetriever._per_parent_fair_select(
            children_by_parent, min_quota=3, total_budget=8
        )
        a_uris = {s["uri"] for s in selected if s["uri"].startswith("a_")}
        self.assertEqual(a_uris, {"a_0"})  # gets its only child


class FrontierSearchTestBase(unittest.TestCase):
    """Base class with real Qdrant setup and tree-building helpers."""

    COLLECTION = "context"

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.qdrant_dir = os.path.join(self.temp_dir, ".qdrant")
        self.config = CortexConfig(
            tenant_id="test_frontier",
            user_id="tester",
            data_root=self.temp_dir,
            embedding_provider="none",
            embedding_dimension=128,
        )
        init_config(self.config)
        self.embedder = MockEmbedder()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    async def _create_storage(self) -> StorageSpy:
        storage = StorageSpy(path=self.qdrant_dir, embedding_dim=128)
        if not await storage.collection_exists(self.COLLECTION):
            schema = {
                "CollectionName": self.COLLECTION,
                "Description": "Test collection for frontier search",
                "Fields": [
                    {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
                    {"FieldName": "uri", "FieldType": "path"},
                    {"FieldName": "parent_uri", "FieldType": "path"},
                    {"FieldName": "context_type", "FieldType": "string"},
                    {"FieldName": "category", "FieldType": "string"},
                    {"FieldName": "abstract", "FieldType": "string"},
                    {"FieldName": "is_leaf", "FieldType": "bool"},
                    {"FieldName": "reward_score", "FieldType": "float"},
                    {"FieldName": "vector", "FieldType": "vector", "Dim": 128},
                ],
                "ScalarIndex": [
                    "uri", "parent_uri", "context_type", "is_leaf", "category",
                ],
            }
            await storage.create_collection(self.COLLECTION, schema)
        return storage

    async def _upsert_node(self, storage, uri, parent_uri, abstract, is_leaf=True,
                           context_type="memory", category="test"):
        """Upsert a single node into Qdrant with real vectors."""
        vector = self.embedder.embed(abstract).dense_vector
        await storage.upsert(
            collection=self.COLLECTION,
            data={
                "id": uri,
                "vector": vector,
                "uri": uri,
                "parent_uri": parent_uri,
                "abstract": abstract,
                "is_leaf": is_leaf,
                "context_type": context_type,
                "category": category,
                "reward_score": 0.0,
            },
        )

    async def _build_flat_tree(self, storage, root_uri, leaf_count=5):
        """Build a 1-level flat tree: root with N leaves."""
        await self._upsert_node(storage, root_uri, "", f"root {root_uri}", is_leaf=False)
        for i in range(leaf_count):
            leaf_uri = f"{root_uri}/leaf_{i}"
            await self._upsert_node(storage, leaf_uri, root_uri, f"leaf {i} content about topic {i}")

    def _make_query(self, text, target_dirs=None):
        return TypedQuery(
            query=text,
            context_type=ContextType.MEMORY,
            intent="test query",
            detail_level=DetailLevel.L0,
            target_directories=target_dirs,
        )


class TestFrontierSingleWave(FrontierSearchTestBase):
    """Flat tree completes in 1 wave."""

    def test_frontier_single_wave(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/user/tester/memories"
            await self._build_flat_tree(storage, root, leaf_count=5)

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True, max_waves=8,
            )
            storage.reset_counts()

            result = await retriever.retrieve(
                self._make_query("leaf content", target_dirs=[root]),
                limit=5,
            )
            self.assertGreater(len(result.matched_contexts), 0)
            # 1 global search + 1 wave batch + possibly compensation = <= 4
            self.assertLessEqual(storage.call_counts["search"], 4)

        self._run(_test())


if __name__ == "__main__":
    unittest.main()
