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


if __name__ == "__main__":
    unittest.main()
