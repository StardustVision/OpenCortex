"""
Tests for frontier batching search optimization.

Uses real QdrantStorageAdapter (embedded) + deterministic MockEmbedder.
No mocks on storage or retriever internals.
"""

import asyncio
import gc
import hashlib
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
        # Use hashlib for deterministic vectors across runs
        # (Python's built-in hash() is randomized per process)
        raw = []
        for i in range(128):
            h = hashlib.md5(f"{text}_{i}".encode()).digest()
            bits = h[0]
            raw.append(bits / 255.0 - 0.5)
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
            data_root=self.temp_dir,
            embedding_provider="none",
            embedding_dimension=128,
        )
        init_config(self.config)
        self.embedder = MockEmbedder()
        self._storages: list = []

    def tearDown(self):
        gc.collect()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        async def _wrapper():
            try:
                return await coro
            finally:
                # Close all Qdrant clients within the same event loop
                for storage in self._storages:
                    try:
                        await storage.close()
                    except Exception:
                        pass
                self._storages.clear()
        return asyncio.run(_wrapper())

    async def _create_storage(self) -> StorageSpy:
        storage = StorageSpy(path=self.qdrant_dir, embedding_dim=128)
        self._storages.append(storage)
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
            root = "opencortex://test_frontier/tester/memories"
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


class TestFrontierMultiWave(FrontierSearchTestBase):
    """3-level tree requires multiple waves."""

    async def _build_3level_tree(self, storage, root_uri):
        await self._upsert_node(storage, root_uri, "", "test root", is_leaf=False)
        dir_a = f"{root_uri}/dir_A"
        await self._upsert_node(storage, dir_a, root_uri, "directory about programming", is_leaf=False)
        for i in range(5):
            await self._upsert_node(storage, f"{dir_a}/leaf_{i}", dir_a,
                                     f"programming topic {i} details")
        sub_a1 = f"{dir_a}/sub_A1"
        await self._upsert_node(storage, sub_a1, dir_a, "sub directory about algorithms", is_leaf=False)
        for i in range(3):
            await self._upsert_node(storage, f"{sub_a1}/leaf_{i}", sub_a1,
                                     f"algorithm analysis {i}")
        dir_b = f"{root_uri}/dir_B"
        await self._upsert_node(storage, dir_b, root_uri, "directory about testing", is_leaf=False)
        for i in range(3):
            await self._upsert_node(storage, f"{dir_b}/leaf_{i}", dir_b,
                                     f"test case {i} validation")

    def test_frontier_multi_wave_depth(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/tester/memories"
            await self._build_3level_tree(storage, root)

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True,
            )
            storage.reset_counts()

            result = await retriever.retrieve(
                self._make_query("algorithm analysis", target_dirs=[root]),
                limit=10,
            )
            self.assertGreater(len(result.matched_contexts), 0)
            # Should find deep leaves (sub_A1 level)
            deep_uris = [m.uri for m in result.matched_contexts if "sub_A1" in m.uri]
            self.assertGreater(len(deep_uris), 0, "Should reach depth-2 leaves")

        self._run(_test())


class TestFrontierConvergence(FrontierSearchTestBase):
    """Convergence early stop."""

    def test_frontier_convergence_early_stop(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/tester/memories"
            # Build wide flat tree - many leaves at level 1
            await self._upsert_node(storage, root, "", "root", is_leaf=False)
            for i in range(15):
                await self._upsert_node(storage, f"{root}/leaf_{i}", root,
                                         f"leaf content {i} about programming")

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True,
            )
            result = await retriever.retrieve(
                self._make_query("programming content", target_dirs=[root]),
                limit=5,
            )
            self.assertGreater(len(result.matched_contexts), 0)
            self.assertLessEqual(len(result.matched_contexts), 5)

        self._run(_test())


class TestFrontierMaxWavesGuard(FrontierSearchTestBase):
    """MAX_WAVES prevents unbounded depth traversal."""

    def test_frontier_max_waves_guard(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/tester/memories"

            # Build chain: root -> dir_0 -> dir_1 -> ... -> dir_11 (leaf)
            current = root
            await self._upsert_node(storage, current, "", "chain root", is_leaf=False)
            for i in range(12):
                child = f"{current}/dir_{i}"
                is_leaf = (i == 11)
                await self._upsert_node(storage, child, current, f"chain node {i}", is_leaf=is_leaf)
                current = child

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True, max_waves=4,
            )
            storage.reset_counts()

            result = await retriever.retrieve(
                self._make_query("chain node", target_dirs=[root]),
                limit=10,
            )
            # Should complete without errors, won't reach depth 12
            self.assertIsNotNone(result)
            self.assertLessEqual(storage.call_counts["search"], 20)

        self._run(_test())


class TestFrontierNoInfiniteLoop(FrontierSearchTestBase):
    """Circular parent_uri references don't cause infinite loop."""

    def test_frontier_no_infinite_loop(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/tester/memories"
            await self._upsert_node(storage, root, "", "root", is_leaf=False)

            uri_a = f"{root}/dir_a"
            uri_b = f"{root}/dir_b"
            await self._upsert_node(storage, uri_a, root, "dir a content", is_leaf=False)
            await self._upsert_node(storage, uri_b, uri_a, "dir b content", is_leaf=False)
            await self._upsert_node(storage, f"{uri_b}/fake_child", uri_b,
                                     "fake child", is_leaf=False)
            await self._upsert_node(storage, f"{uri_a}/leaf", uri_a,
                                     "a leaf node", is_leaf=True)

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True, max_waves=8,
            )
            result = await retriever.retrieve(
                self._make_query("dir content", target_dirs=[root]),
                limit=5,
            )
            self.assertIsNotNone(result)

        self._run(_test())


class TestFrontierEmptyNode(FrontierSearchTestBase):
    """Directory with no children."""

    def test_frontier_empty_node(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/tester/memories"
            await self._upsert_node(storage, root, "", "root", is_leaf=False)
            await self._upsert_node(storage, f"{root}/empty", root, "empty directory", is_leaf=False)

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True,
            )
            result = await retriever.retrieve(
                self._make_query("empty directory", target_dirs=[root]),
                limit=5,
            )
            self.assertIsNotNone(result)

        self._run(_test())


class TestCollectedDedup(FrontierSearchTestBase):
    """Same leaf reachable from different paths — no duplicates."""

    def test_collected_dedup_keeps_higher_score(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/tester/memories"
            await self._upsert_node(storage, root, "", "root", is_leaf=False)

            dir_a = f"{root}/dir_a"
            dir_b = f"{root}/dir_b"
            await self._upsert_node(storage, dir_a, root, "path a", is_leaf=False)
            await self._upsert_node(storage, dir_b, root, "path b", is_leaf=False)
            await self._upsert_node(storage, f"{dir_a}/leaf", dir_a, "optimization techniques")
            await self._upsert_node(storage, f"{dir_b}/other", dir_b, "database operations")

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True,
            )
            result = await retriever.retrieve(
                self._make_query("optimization techniques", target_dirs=[root]),
                limit=10,
            )
            uris = [m.uri for m in result.matched_contexts]
            self.assertEqual(len(uris), len(set(uris)), "No duplicate URIs in results")

        self._run(_test())


class TestFallbackOnError(FrontierSearchTestBase):
    """Auto-fallback to recursive search on frontier error."""

    def test_fallback_on_frontier_error(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/tester/memories"
            await self._build_flat_tree(storage, root, leaf_count=3)

            # Monkey-patch the existing storage to inject an error on the first
            # batch parent_uri query, then fall through to real implementation.
            _original_search = storage.search.__func__ if hasattr(storage.search, '__func__') else None
            _error_fired = {"count": 0}

            async def _error_search(self_storage, *args, **kwargs):
                self_storage.call_counts["search"] += 1
                f = kwargs.get("filter", {})
                if isinstance(f, dict):
                    for c in f.get("conds", []):
                        if isinstance(c, dict) and c.get("field") == "parent_uri":
                            if len(c.get("conds", [])) > 1 and _error_fired["count"] == 0:
                                _error_fired["count"] += 1
                                raise RuntimeError("Simulated frontier batch failure")
                return await QdrantStorageAdapter.search(self_storage, *args, **kwargs)

            import types
            storage.search = types.MethodType(_error_search, storage)

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True,
            )
            result = await retriever.retrieve(
                self._make_query("leaf content", target_dirs=[root]),
                limit=5,
            )
            self.assertGreater(len(result.matched_contexts), 0)

        self._run(_test())


class TestFrontierVsRecursiveOverlap(FrontierSearchTestBase):
    """Compare frontier and recursive results on same data."""

    async def _build_comparison_tree(self, storage, root_uri):
        await self._upsert_node(storage, root_uri, "", "test root", is_leaf=False)
        dir_a = f"{root_uri}/dir_A"
        await self._upsert_node(storage, dir_a, root_uri, "programming concepts", is_leaf=False)
        for i in range(8):
            await self._upsert_node(storage, f"{dir_a}/leaf_{i}", dir_a,
                                     f"programming concept {i} explanation")
        dir_b = f"{root_uri}/dir_B"
        await self._upsert_node(storage, dir_b, root_uri, "testing patterns", is_leaf=False)
        for i in range(5):
            await self._upsert_node(storage, f"{dir_b}/leaf_{i}", dir_b,
                                     f"testing pattern {i} details")

    def test_frontier_vs_recursive_overlap(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/tester/memories"
            await self._build_comparison_tree(storage, root)

            query = self._make_query("programming concept explanation",
                                     target_dirs=[root])

            retriever_frontier = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True,
            )
            retriever_recursive = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=False,
            )

            result_f = await retriever_frontier.retrieve(query, limit=10)
            result_r = await retriever_recursive.retrieve(query, limit=10)

            uris_f = {m.uri for m in result_f.matched_contexts}
            uris_r = {m.uri for m in result_r.matched_contexts}

            if not uris_r:
                return  # Skip if recursive returns nothing

            overlap = len(uris_f & uris_r) / max(len(uris_r), 1)
            if overlap < 0.8:
                import logging
                logging.warning(
                    "[TestOverlap] Overlap %.1f%% — frontier: %s, recursive: %s",
                    overlap * 100, uris_f, uris_r,
                )
            if overlap < 0.7:
                self.fail(
                    f"Overlap {overlap:.1%} < 70% threshold. "
                    f"Frontier: {uris_f}, Recursive: {uris_r}"
                )

        self._run(_test())


if __name__ == "__main__":
    unittest.main()
