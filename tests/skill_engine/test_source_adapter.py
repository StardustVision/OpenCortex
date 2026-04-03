"""Tests for QdrantSourceAdapter and cosine similarity."""

import unittest
from unittest.mock import AsyncMock, MagicMock

from opencortex.skill_engine.adapters.source_adapter import (
    QdrantSourceAdapter, MemoryCluster, MemoryRecord,
)
from opencortex.utils.similarity import cosine_similarity


class TestCosineSimilarity(unittest.TestCase):

    def test_identical_vectors(self):
        self.assertAlmostEqual(cosine_similarity([1, 0, 0], [1, 0, 0]), 1.0)

    def test_orthogonal_vectors(self):
        self.assertAlmostEqual(cosine_similarity([1, 0, 0], [0, 1, 0]), 0.0)

    def test_empty_vectors(self):
        self.assertEqual(cosine_similarity([], []), 0.0)

    def test_mismatched_length(self):
        self.assertEqual(cosine_similarity([1, 2], [1, 2, 3]), 0.0)


class TestQdrantSourceAdapter(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.storage = AsyncMock()
        self.embedder = MagicMock()
        self.embedder.embed = MagicMock(
            return_value=MagicMock(dense_vector=[0.1, 0.2, 0.3, 0.4])
        )
        self.adapter = QdrantSourceAdapter(
            storage=self.storage, embedder=self.embedder,
        )

    def _make_memory_records(self, count=5, context_type="memory", category="events"):
        return [
            {
                "id": f"m{i}",
                "uri": f"opencortex://t/u/memories/events/m{i}",
                "abstract": f"Memory about deployment step {i}",
                "overview": f"Overview {i}",
                "context_type": context_type,
                "category": category,
                "source_tenant_id": "team1",
                "source_user_id": "hugo",
                "scope": "private",
                "is_leaf": True,
                "reward_score": 0.5,
            }
            for i in range(count)
        ]

    async def test_scan_returns_clusters(self):
        """scan_memories returns clusters when enough similar memories exist."""
        records = self._make_memory_records(count=5)
        self.storage.filter = AsyncMock(return_value=records)

        clusters = await self.adapter.scan_memories("team1", "hugo", min_count=3)
        self.storage.filter.assert_called_once()
        # All records have same embedder mock -> all similar -> one cluster
        self.assertGreaterEqual(len(clusters), 1)
        self.assertGreaterEqual(len(clusters[0].memory_ids), 3)

    async def test_scan_skips_small_clusters(self):
        """Clusters with < min_count are skipped."""
        records = self._make_memory_records(count=2)
        self.storage.filter = AsyncMock(return_value=records)

        clusters = await self.adapter.scan_memories("team1", "hugo", min_count=3)
        self.assertEqual(len(clusters), 0)

    async def test_scan_empty_collection(self):
        """Empty collection returns empty list."""
        self.storage.filter = AsyncMock(return_value=[])
        clusters = await self.adapter.scan_memories("team1", "hugo")
        self.assertEqual(len(clusters), 0)

    async def test_scan_applies_context_type_filter(self):
        """context_types parameter is added to filter."""
        self.storage.filter = AsyncMock(return_value=[])
        await self.adapter.scan_memories("team1", "hugo", context_types=["memory"])
        call_args = self.storage.filter.call_args
        filter_expr = call_args[0][1]
        # Should contain context_type filter in conds
        conds_str = str(filter_expr)
        self.assertIn("context_type", conds_str)

    async def test_get_cluster_memories(self):
        """get_cluster_memories returns MemoryRecord objects."""
        raw = self._make_memory_records(count=3)
        self.storage.get = AsyncMock(return_value=raw)

        cluster = MemoryCluster(
            cluster_id="cl-1", theme="test",
            memory_ids=["m0", "m1", "m2"],
            centroid_embedding=[0.1] * 4, avg_score=0.5,
        )
        records = await self.adapter.get_cluster_memories(cluster)
        self.assertEqual(len(records), 3)
        self.assertIsInstance(records[0], MemoryRecord)
        self.assertEqual(records[0].memory_id, "m0")

    async def test_cluster_groups_by_context_type(self):
        """Different context_types are grouped separately."""
        records = (
            self._make_memory_records(count=3, context_type="memory", category="events")
            + self._make_memory_records(count=3, context_type="resource", category="documents")
        )
        # Make resource IDs unique
        for i, r in enumerate(records[3:]):
            r["id"] = f"r{i}"
        self.storage.filter = AsyncMock(return_value=records)

        clusters = await self.adapter.scan_memories("team1", "hugo", min_count=3)
        # Should have 2 clusters (one per group)
        self.assertEqual(len(clusters), 2)


if __name__ == "__main__":
    unittest.main()
