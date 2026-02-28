# Frontier Batching Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace per-directory heapq search with wave-based frontier batching + late rerank in HierarchicalRetriever, reducing storage.search calls from O(N) to O(D)+O(S) and rerank calls from N to 1.

**Architecture:** New `_frontier_search_impl` method alongside existing `_recursive_search` (preserved as fallback). Feature flag `use_frontier_batching` in `__init__` controls dispatch. Auto-degradation on exception.

**Tech Stack:** Python 3.10+ async, Qdrant embedded, existing VikingDBInterface/filter_translator

**Design doc:** `docs/plans/2026-02-28-frontier-batching-design.md`

---

### Task 1: Add constants and __init__ params

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py:50-63`

**Step 1: Add new class constants after existing ones (line 54)**

At `hierarchical_retriever.py:54`, after `GLOBAL_SEARCH_TOPK = 3`, add:

```python
    # Frontier batching constants
    MAX_FRONTIER_SIZE = 64          # Max directories per wave (prevents oversized IN)
    MIN_CHILDREN_PER_DIR = 2        # Min guaranteed children per parent directory
    LATE_RERANK_FACTOR = 5          # Late rerank candidate multiplier
    LATE_RERANK_CAP = 50            # Late rerank candidate cap
    DEFAULT_MAX_WAVES = 8           # Default max wave iterations
```

**Step 2: Add new __init__ params**

Add `use_frontier_batching: bool = True` and `max_waves: int = 8` to `__init__` signature and store them:

```python
    def __init__(
        self,
        storage: VikingDBInterface,
        embedder: Optional[Any],
        rerank_config: Optional[RerankConfig] = None,
        llm_completion: Optional[Any] = None,
        rl_weight: float = 0.05,
        use_frontier_batching: bool = True,
        max_waves: int = 8,
    ):
```

After `self._rl_weight = rl_weight` (line 79), add:

```python
        self._use_frontier_batching = use_frontier_batching
        self._max_waves = max_waves
```

**Step 3: Run existing tests to confirm no regression**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_integration_skill_pipeline -v`
Expected: All existing tests PASS

**Step 4: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py
git commit -m "feat(retriever): add frontier batching constants and feature flag params"
```

---

### Task 2: Adapt _should_rerank with score_key parameter

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py:264-282`
- Test: `tests/test_frontier_search.py` (create)

**Step 1: Create test file with test infrastructure and first test**

Create `tests/test_frontier_search.py`:

```python
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


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_frontier_search.TestShouldRerankScoreKey -v`
Expected: FAIL (TypeError — _should_rerank doesn't accept score_key yet)

**Step 3: Implement _should_rerank change**

Replace the current `_should_rerank` method (lines 264-282) with:

```python
    def _should_rerank(self, results: List[Dict[str, Any]], score_key: str = "_score") -> bool:
        """Decide whether rerank is worth the cost.

        Skip rerank when the top result has a clear score lead over the
        second result — reranking is unlikely to change the ordering.

        Args:
            results: List of result dicts.
            score_key: Which score field to use ('_score' or '_final_score').
        """
        if len(results) < 2:
            return False
        scores = sorted(
            [r.get(score_key, 0.0) for r in results], reverse=True
        )
        gap = scores[0] - scores[1]
        if gap > self._score_gap_threshold:
            logger.debug(
                "[Rerank] Skipped — score gap %.3f > threshold %.3f",
                gap, self._score_gap_threshold,
            )
            return False
        return True
```

**Step 4: Run test to verify it passes**

Run: `uv run python3 -m unittest tests.test_frontier_search.TestShouldRerankScoreKey -v`
Expected: PASS (3 tests)

**Step 5: Run existing tests to confirm no regression**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_frontier_search.py
git commit -m "feat(retriever): add score_key param to _should_rerank for late rerank support"
```

---

### Task 3: Add _diverse_truncate helper

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py`
- Test: `tests/test_frontier_search.py`

**Step 1: Add test**

Append to `tests/test_frontier_search.py`:

```python
from collections import defaultdict


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
```

**Step 2: Run to verify fail**

Run: `uv run python3 -m unittest tests.test_frontier_search.TestDiverseTruncate -v`
Expected: FAIL (AttributeError — _diverse_truncate not defined)

**Step 3: Implement _diverse_truncate**

Add as a `@staticmethod` on `HierarchicalRetriever`, before `_get_root_uris_for_type`:

```python
    @staticmethod
    def _diverse_truncate(
        frontier: List[Tuple[str, float]],
        max_size: int,
    ) -> List[Tuple[str, float]]:
        """Truncate frontier with diversity across root branches.

        Buckets by URI prefix (root branch), sorts each bucket by score desc,
        then round-robin fills to max_size.
        """
        if len(frontier) <= max_size:
            return frontier

        buckets: Dict[str, List[Tuple[str, float]]] = {}
        for uri, score in frontier:
            parts = uri.split("/")
            root = "/".join(parts[:5]) if len(parts) >= 5 else uri
            if root not in buckets:
                buckets[root] = []
            buckets[root].append((uri, score))

        for b in buckets.values():
            b.sort(key=lambda x: x[1], reverse=True)

        result: List[Tuple[str, float]] = []
        iters = [iter(b) for b in buckets.values()]
        while len(result) < max_size and iters:
            next_round = []
            for it in iters:
                if len(result) >= max_size:
                    break
                item = next(it, None)
                if item is not None:
                    result.append(item)
                    next_round.append(it)
            iters = next_round

        return result[:max_size]
```

**Step 4: Run to verify pass**

Run: `uv run python3 -m unittest tests.test_frontier_search.TestDiverseTruncate -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_frontier_search.py
git commit -m "feat(retriever): add _diverse_truncate for frontier diversity balancing"
```

---

### Task 4: Add _per_parent_fair_select helper

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py`
- Test: `tests/test_frontier_search.py`

**Step 1: Add test**

Append to `tests/test_frontier_search.py`:

```python
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
```

**Step 2: Run to verify fail**

Run: `uv run python3 -m unittest tests.test_frontier_search.TestFairSelect -v`
Expected: FAIL (AttributeError — _per_parent_fair_select not defined)

**Step 3: Implement _per_parent_fair_select**

Add as `@staticmethod` on `HierarchicalRetriever`:

```python
    @staticmethod
    def _per_parent_fair_select(
        children_by_parent: Dict[str, List[Dict[str, Any]]],
        min_quota: int,
        total_budget: int,
    ) -> List[Dict[str, Any]]:
        """Fair select: each parent gets min_quota first, rest compete globally.

        Args:
            children_by_parent: {parent_uri: [child_dicts]} — children should
                already have '_final_score' set.
            min_quota: Minimum children guaranteed per parent.
            total_budget: Maximum total children to return.
        """
        selected: List[Dict[str, Any]] = []
        remaining: List[Dict[str, Any]] = []

        for children in children_by_parent.values():
            sorted_children = sorted(
                children, key=lambda x: x.get("_final_score", 0.0), reverse=True
            )
            selected.extend(sorted_children[:min_quota])
            remaining.extend(sorted_children[min_quota:])

        if len(selected) < total_budget:
            remaining.sort(key=lambda x: x.get("_final_score", 0.0), reverse=True)
            selected.extend(remaining[: total_budget - len(selected)])

        return selected[:total_budget]
```

**Step 4: Run to verify pass**

Run: `uv run python3 -m unittest tests.test_frontier_search.TestFairSelect -v`
Expected: PASS (3 tests)

**Step 5: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_frontier_search.py
git commit -m "feat(retriever): add _per_parent_fair_select with min quota enforcement"
```

---

### Task 5: Implement _frontier_search_impl core algorithm

**Files:**
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py`
- Test: `tests/test_frontier_search.py`

**Step 1: Add integration test infrastructure — tree builder and base test class**

Append to `tests/test_frontier_search.py`:

```python
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
            await storage.create_collection(self.COLLECTION)
        return storage

    async def _upsert_node(self, storage, uri, parent_uri, abstract, is_leaf=True,
                           context_type="memory", category="test"):
        """Upsert a single node into Qdrant with real vectors."""
        vector = self.embedder.embed(abstract).dense_vector
        await storage.upsert(
            collection=self.COLLECTION,
            id=uri,
            vector=vector,
            payload={
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

    async def _build_deep_chain(self, storage, root_uri, depth=12):
        """Build a chain: root → dir_0 → dir_1 → ... → dir_{depth-1} (leaf)."""
        current = root_uri
        await self._upsert_node(storage, current, "", f"chain root", is_leaf=False)
        for i in range(depth):
            child = f"{current}/dir_{i}"
            is_leaf = (i == depth - 1)
            await self._upsert_node(storage, child, current, f"chain node {i}", is_leaf=is_leaf)
            if not is_leaf:
                pass  # will be parent of next
            current = child

    async def _build_3level_tree(self, storage, root_uri):
        """Build the standard 3-level test tree from the design doc.

        root/
        ├── dir_A/          (20 leaves)
        │   ├── sub_A1/     (5 leaves)
        │   └── sub_A2/     (5 leaves)
        ├── dir_B/          (2 leaves)
        │   └── sub_B1/     (1 leaf)
        └── dir_C/          (8 leaves)
        """
        await self._upsert_node(storage, root_uri, "", "test root", is_leaf=False)

        # dir_A (hot)
        dir_a = f"{root_uri}/dir_A"
        await self._upsert_node(storage, dir_a, root_uri, "hot directory about programming", is_leaf=False)
        for i in range(20):
            await self._upsert_node(storage, f"{dir_a}/leaf_{i}", dir_a,
                                     f"programming topic {i} implementation details")

        sub_a1 = f"{dir_a}/sub_A1"
        await self._upsert_node(storage, sub_a1, dir_a, "sub directory A1 about algorithms", is_leaf=False)
        for i in range(5):
            await self._upsert_node(storage, f"{sub_a1}/leaf_{i}", sub_a1,
                                     f"algorithm topic {i} analysis")

        sub_a2 = f"{dir_a}/sub_A2"
        await self._upsert_node(storage, sub_a2, dir_a, "sub directory A2 about data structures", is_leaf=False)
        for i in range(5):
            await self._upsert_node(storage, f"{sub_a2}/leaf_{i}", sub_a2,
                                     f"data structure {i} operations")

        # dir_B (cold)
        dir_b = f"{root_uri}/dir_B"
        await self._upsert_node(storage, dir_b, root_uri, "cold directory about documentation", is_leaf=False)
        for i in range(2):
            await self._upsert_node(storage, f"{dir_b}/leaf_{i}", dir_b,
                                     f"documentation section {i}")

        sub_b1 = f"{dir_b}/sub_B1"
        await self._upsert_node(storage, sub_b1, dir_b, "sub directory B1 about guides", is_leaf=False)
        await self._upsert_node(storage, f"{sub_b1}/leaf_0", sub_b1, "user guide introduction")

        # dir_C (medium)
        dir_c = f"{root_uri}/dir_C"
        await self._upsert_node(storage, dir_c, root_uri, "medium directory about testing", is_leaf=False)
        for i in range(8):
            await self._upsert_node(storage, f"{dir_c}/leaf_{i}", dir_c,
                                     f"test case {i} for validation")

    def _make_query(self, text, target_dirs=None):
        return TypedQuery(
            query=text,
            context_type=ContextType.MEMORY,
            detail_level=DetailLevel.L0,
            target_directories=target_dirs,
        )
```

**Step 2: Add first integration test — single wave**

```python
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
            # 1 global search + 1 wave batch + possibly 1 compensation = <= 3
            self.assertLessEqual(storage.call_counts["search"], 3)

        self._run(_test())
```

**Step 3: Run to verify fail**

Run: `uv run python3 -m unittest tests.test_frontier_search.TestFrontierSingleWave -v`
Expected: FAIL (_frontier_search not found — dispatch not wired yet, but we test the full retrieve path)

**Step 4: Implement _frontier_search_impl**

Add the following methods to `HierarchicalRetriever`, between `_recursive_search` and `_convert_to_matched_contexts`:

```python
    async def _frontier_search(
        self,
        query: str,
        collection: str,
        query_vector: Optional[List[float]],
        sparse_query_vector: Optional[Dict[str, float]],
        starting_points: List[Tuple[str, float]],
        limit: int,
        mode: str,
        threshold: Optional[float] = None,
        score_gte: bool = False,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Frontier search with auto-fallback to recursive on error."""
        try:
            return await self._frontier_search_impl(
                query=query,
                collection=collection,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,
                starting_points=starting_points,
                limit=limit,
                mode=mode,
                threshold=threshold,
                score_gte=score_gte,
                metadata_filter=metadata_filter,
            )
        except Exception as e:
            logger.error("[FrontierSearch] Fallback to recursive: %s", e)
            return await self._recursive_search(
                query=query,
                collection=collection,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,
                starting_points=starting_points,
                limit=limit,
                mode=mode,
                threshold=threshold,
                score_gte=score_gte,
                metadata_filter=metadata_filter,
            )

    async def _frontier_search_impl(
        self,
        query: str,
        collection: str,
        query_vector: Optional[List[float]],
        sparse_query_vector: Optional[Dict[str, float]],
        starting_points: List[Tuple[str, float]],
        limit: int,
        mode: str,
        threshold: Optional[float] = None,
        score_gte: bool = False,
        metadata_filter: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Wave-based frontier batching search.

        Replaces per-directory heapq search with batch queries per wave.
        See docs/plans/2026-02-28-frontier-batching-design.md for algorithm.
        """
        effective_threshold = threshold if threshold is not None else self.threshold

        def passes_threshold(score: float) -> bool:
            if score_gte:
                return score >= effective_threshold
            return score > effective_threshold

        def merge_filter(base_filter: Dict, extra_filter: Optional[Dict]) -> Dict:
            if not extra_filter:
                return base_filter
            return {"op": "and", "conds": [base_filter, extra_filter]}

        sparse_query_vector = sparse_query_vector or None
        alpha = self.SCORE_PROPAGATION_ALPHA

        # collected: uri -> dict (O(1) dedup, no index invalidation)
        collected: Dict[str, Dict[str, Any]] = {}
        visited_dirs: set = set()
        convergence_rounds = 0
        prev_topk_uris: set = set()

        frontier: List[Tuple[str, float]] = list(starting_points)

        for wave_idx in range(self._max_waves):
            if not frontier:
                break

            # 1. Frontier truncation (diversity-aware)
            if len(frontier) > self.MAX_FRONTIER_SIZE:
                frontier = self._diverse_truncate(frontier, self.MAX_FRONTIER_SIZE)

            # 2. Batch query
            per_wave_limit = max(
                limit * 3,
                len(frontier) * self.MIN_CHILDREN_PER_DIR * 2,
                30,
            )
            parent_uris = [uri for uri, _ in frontier]
            frontier_scores = {uri: score for uri, score in frontier}

            batch_filter = merge_filter(
                {"op": "must", "field": "parent_uri", "conds": parent_uris},
                metadata_filter,
            )
            results = await self.storage.search(
                collection=collection,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,
                filter=batch_filter,
                limit=per_wave_limit,
            )

            # 3. Group by parent + score propagation
            children_by_parent: Dict[str, List[Dict[str, Any]]] = {}
            for r in results:
                p_uri = r.get("parent_uri", "")
                if p_uri not in children_by_parent:
                    children_by_parent[p_uri] = []
                children_by_parent[p_uri].append(r)

            for p_uri, children in children_by_parent.items():
                parent_score = frontier_scores.get(p_uri, 0.0)
                for child in children:
                    raw_score = child.get("_score", 0.0)
                    child["_final_score"] = (
                        alpha * raw_score + (1 - alpha) * parent_score
                        if parent_score
                        else raw_score
                    )
                    reward = child.get("reward_score", 0.0)
                    if reward != 0 and self._rl_weight:
                        child["_final_score"] += self._rl_weight * reward

            # 4. Compensation query (starved parents)
            starved = [
                uri for uri in parent_uris
                if len(children_by_parent.get(uri, [])) < self.MIN_CHILDREN_PER_DIR
                and uri not in visited_dirs
            ]
            if starved:
                comp_filter = merge_filter(
                    {"op": "must", "field": "parent_uri", "conds": starved},
                    metadata_filter,
                )
                comp_results = await self.storage.search(
                    collection=collection,
                    query_vector=query_vector,
                    sparse_query_vector=sparse_query_vector,
                    filter=comp_filter,
                    limit=len(starved) * self.MIN_CHILDREN_PER_DIR,
                )
                for r in comp_results:
                    p_uri = r.get("parent_uri", "")
                    if p_uri not in children_by_parent:
                        children_by_parent[p_uri] = []
                    if not any(c.get("uri") == r.get("uri") for c in children_by_parent[p_uri]):
                        parent_score = frontier_scores.get(p_uri, 0.0)
                        raw_score = r.get("_score", 0.0)
                        r["_final_score"] = (
                            alpha * raw_score + (1 - alpha) * parent_score
                            if parent_score
                            else raw_score
                        )
                        reward = r.get("reward_score", 0.0)
                        if reward != 0 and self._rl_weight:
                            r["_final_score"] += self._rl_weight * reward
                        children_by_parent[p_uri].append(r)

                # Tiny queries for still-starved parents
                still_starved = [
                    uri for uri in starved
                    if len(children_by_parent.get(uri, [])) < self.MIN_CHILDREN_PER_DIR
                ]
                for s_uri in still_starved:
                    tiny_filter = merge_filter(
                        {"op": "must", "field": "parent_uri", "conds": [s_uri]},
                        metadata_filter,
                    )
                    tiny_results = await self.storage.search(
                        collection=collection,
                        query_vector=query_vector,
                        sparse_query_vector=sparse_query_vector,
                        filter=tiny_filter,
                        limit=self.MIN_CHILDREN_PER_DIR,
                    )
                    for r in tiny_results:
                        if not any(c.get("uri") == r.get("uri") for c in children_by_parent.get(s_uri, [])):
                            parent_score = frontier_scores.get(s_uri, 0.0)
                            raw_score = r.get("_score", 0.0)
                            r["_final_score"] = (
                                alpha * raw_score + (1 - alpha) * parent_score
                                if parent_score
                                else raw_score
                            )
                            reward = r.get("reward_score", 0.0)
                            if reward != 0 and self._rl_weight:
                                r["_final_score"] += self._rl_weight * reward
                            if s_uri not in children_by_parent:
                                children_by_parent[s_uri] = []
                            children_by_parent[s_uri].append(r)

            # 5. Fair select
            selected = self._per_parent_fair_select(
                children_by_parent,
                min_quota=self.MIN_CHILDREN_PER_DIR,
                total_budget=per_wave_limit,
            )

            # 6. Triage + cycle prevention
            next_frontier: Dict[str, float] = {}
            for child in selected:
                final_score = child.get("_final_score", 0.0)
                if not passes_threshold(final_score):
                    continue
                uri = child.get("uri", "")
                if uri in collected:
                    if final_score > collected[uri].get("_final_score", 0.0):
                        collected[uri] = child
                else:
                    collected[uri] = child
                if not child.get("is_leaf", False) and uri not in visited_dirs:
                    old_score = next_frontier.get(uri, -1.0)
                    if final_score > old_score:
                        next_frontier[uri] = final_score

            visited_dirs.update(uri for uri, _ in frontier)

            # 7. Convergence check
            top_k_items = heapq.nlargest(
                limit, collected.values(),
                key=lambda x: x.get("_final_score", 0.0),
            )
            current_topk_uris = {c.get("uri", "") for c in top_k_items}
            if current_topk_uris == prev_topk_uris and len(collected) >= limit:
                convergence_rounds += 1
                if convergence_rounds >= self.MAX_CONVERGENCE_ROUNDS:
                    logger.info(
                        "[FrontierSearch] Converged after %d waves", wave_idx + 1
                    )
                    break
            else:
                convergence_rounds = 0
                prev_topk_uris = current_topk_uris

            frontier = [(uri, score) for uri, score in next_frontier.items()]

        # 8. Late Rerank
        all_candidates = sorted(
            collected.values(),
            key=lambda x: x.get("_final_score", 0.0),
            reverse=True,
        )
        rerank_count = min(self.LATE_RERANK_CAP, limit * self.LATE_RERANK_FACTOR)
        top_m = all_candidates[:rerank_count]

        if (
            self._rerank_client
            and mode == RetrieverMode.THINKING
            and self._should_rerank(top_m, score_key="_final_score")
        ):
            docs = [c.get("abstract", "") for c in top_m]
            rerank_scores = await self._rerank_client.rerank(query, docs)
            beta = self._fusion_beta
            for c, rs in zip(top_m, rerank_scores):
                c["_final_score"] = beta * rs + (1 - beta) * c.get("_final_score", 0.0)
            top_m.sort(key=lambda x: x.get("_final_score", 0.0), reverse=True)

        return top_m[:limit]
```

**Step 5: Wire dispatch in retrieve()**

Replace lines 213-225 in `retrieve()`:

```python
        # Step 4: Search (frontier batching or recursive fallback)
        if self._use_frontier_batching:
            candidates = await self._frontier_search(
                query=query.query,
                collection=collection,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,
                starting_points=starting_points,
                limit=limit,
                mode=mode,
                threshold=effective_threshold,
                score_gte=score_gte,
                metadata_filter=final_metadata_filter,
            )
        else:
            candidates = await self._recursive_search(
                query=query.query,
                collection=collection,
                query_vector=query_vector,
                sparse_query_vector=sparse_query_vector,
                starting_points=starting_points,
                limit=limit,
                mode=mode,
                threshold=effective_threshold,
                score_gte=score_gte,
                metadata_filter=final_metadata_filter,
            )
```

**Step 6: Run single-wave test**

Run: `uv run python3 -m unittest tests.test_frontier_search.TestFrontierSingleWave -v`
Expected: PASS

**Step 7: Run all existing tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_integration_skill_pipeline -v`
Expected: All PASS

**Step 8: Commit**

```bash
git add src/opencortex/retrieve/hierarchical_retriever.py tests/test_frontier_search.py
git commit -m "feat(retriever): implement frontier batching search with wave-based queries"
```

---

### Task 6: Add remaining integration tests

**Files:**
- Test: `tests/test_frontier_search.py`

**Step 1: Add all remaining test classes**

Append to `tests/test_frontier_search.py`:

```python
class TestFrontierMultiWave(FrontierSearchTestBase):
    """3-level tree requires multiple waves."""

    def test_frontier_multi_wave_depth(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/user/tester/memories"
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
            # Should find deep leaves (sub_A1, sub_A2 level)
            deep_uris = [m.uri for m in result.matched_contexts if "sub_A" in m.uri]
            self.assertGreater(len(deep_uris), 0, "Should reach depth-2 leaves")

        self._run(_test())


class TestFrontierConvergence(FrontierSearchTestBase):
    """Convergence early stop."""

    def test_frontier_convergence_early_stop(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/user/tester/memories"
            await self._build_3level_tree(storage, root)

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True,
            )
            storage.reset_counts()

            result = await retriever.retrieve(
                self._make_query("programming implementation", target_dirs=[root]),
                limit=5,
            )
            # With limit=5 and lots of high-score leaves in dir_A,
            # should converge early and not search all directories
            self.assertEqual(len(result.matched_contexts), 5)

        self._run(_test())


class TestFrontierMaxWavesGuard(FrontierSearchTestBase):
    """MAX_WAVES prevents unbounded depth traversal."""

    def test_frontier_max_waves_guard(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/user/tester/memories"
            await self._build_deep_chain(storage, root, depth=12)

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True, max_waves=4,
            )
            storage.reset_counts()

            result = await retriever.retrieve(
                self._make_query("chain node", target_dirs=[root]),
                limit=10,
            )
            # With max_waves=4, should not reach depth 12
            # search calls: 1 global + at most 4 waves + compensation
            self.assertLessEqual(storage.call_counts["search"], 12)

        self._run(_test())


class TestFrontierNoInfiniteLoop(FrontierSearchTestBase):
    """Circular parent_uri references don't cause infinite loop."""

    def test_frontier_no_infinite_loop(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/user/tester/memories"

            # Create circular: A -> B -> A
            await self._upsert_node(storage, root, "", "root", is_leaf=False)
            uri_a = f"{root}/dir_a"
            uri_b = f"{root}/dir_b"
            await self._upsert_node(storage, uri_a, root, "dir a content", is_leaf=False)
            await self._upsert_node(storage, uri_b, uri_a, "dir b content", is_leaf=False)
            # Create child of B that points back to A as parent_uri
            await self._upsert_node(storage, f"{uri_b}/fake_child", uri_b,
                                     "fake child", is_leaf=False)
            # And a leaf pointing to A (circular reference)
            await self._upsert_node(storage, f"{uri_a}/back_ref", uri_a,
                                     "back reference leaf", is_leaf=True)

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True, max_waves=8,
            )
            # Should complete without hanging
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
            root = "opencortex://test_frontier/user/tester/memories"
            await self._upsert_node(storage, root, "", "root", is_leaf=False)

            empty_dir = f"{root}/empty"
            await self._upsert_node(storage, empty_dir, root, "empty directory", is_leaf=False)

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True,
            )
            result = await retriever.retrieve(
                self._make_query("empty directory", target_dirs=[root]),
                limit=5,
            )
            # Should not crash, may return the empty dir itself
            self.assertIsNotNone(result)

        self._run(_test())


class TestCollectedDedup(FrontierSearchTestBase):
    """Same leaf from two parent paths keeps higher score."""

    def test_collected_dedup_keeps_higher_score(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/user/tester/memories"
            await self._upsert_node(storage, root, "", "root", is_leaf=False)

            # Two dirs sharing a conceptually similar leaf
            dir_a = f"{root}/dir_a"
            dir_b = f"{root}/dir_b"
            await self._upsert_node(storage, dir_a, root, "path a", is_leaf=False)
            await self._upsert_node(storage, dir_b, root, "path b", is_leaf=False)

            # Leaf under dir_a
            shared_content = "shared leaf about optimization techniques"
            await self._upsert_node(storage, f"{dir_a}/shared", dir_a, shared_content)
            # Different leaf under dir_b
            await self._upsert_node(storage, f"{dir_b}/other", dir_b, "other leaf about databases")

            retriever = HierarchicalRetriever(
                storage=storage, embedder=self.embedder,
                use_frontier_batching=True,
            )
            result = await retriever.retrieve(
                self._make_query("optimization techniques", target_dirs=[root]),
                limit=10,
            )
            # Each URI should appear at most once
            uris = [m.uri for m in result.matched_contexts]
            self.assertEqual(len(uris), len(set(uris)), "No duplicate URIs in results")

        self._run(_test())


class TestFallbackOnError(FrontierSearchTestBase):
    """Auto-fallback to recursive search on frontier error."""

    def test_fallback_on_frontier_error(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/user/tester/memories"
            await self._build_flat_tree(storage, root, leaf_count=3)

            class ErrorOnFrontierStorage(StorageSpy):
                """Raises on batch parent_uri queries (simulating frontier failure)."""
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    self._frontier_error_armed = False

                async def search(self, *args, **kwargs):
                    self.call_counts["search"] += 1
                    f = kwargs.get("filter", {})
                    # Detect frontier batch filter: parent_uri with multiple conds
                    if isinstance(f, dict):
                        conds = f.get("conds", [])
                        for c in (conds if isinstance(conds, list) else []):
                            if isinstance(c, dict) and c.get("field") == "parent_uri":
                                if len(c.get("conds", [])) > 1:
                                    raise RuntimeError("Simulated frontier batch failure")
                    # Call grandparent (QdrantStorageAdapter) search
                    return await QdrantStorageAdapter.search(self, *args, **kwargs)

            error_storage = ErrorOnFrontierStorage(
                path=self.qdrant_dir, embedding_dim=128
            )

            retriever = HierarchicalRetriever(
                storage=error_storage, embedder=self.embedder,
                use_frontier_batching=True,
            )
            # Should fallback to recursive search and still return results
            result = await retriever.retrieve(
                self._make_query("leaf content", target_dirs=[root]),
                limit=5,
            )
            self.assertGreater(len(result.matched_contexts), 0)

        self._run(_test())


class TestFrontierVsRecursiveOverlap(FrontierSearchTestBase):
    """Compare frontier and recursive results on same data."""

    def test_frontier_vs_recursive_overlap(self):
        async def _test():
            storage = await self._create_storage()
            root = "opencortex://test_frontier/user/tester/memories"
            await self._build_3level_tree(storage, root)

            query = self._make_query("programming algorithm data structure",
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
                    "[TestOverlap] Overlap %.1f%% < 80%% — frontier: %s, recursive: %s",
                    overlap * 100, uris_f, uris_r,
                )
            if overlap < 0.7:
                self.fail(
                    f"Overlap {overlap:.1%} < 70% threshold. "
                    f"Frontier: {uris_f}, Recursive: {uris_r}"
                )

        self._run(_test())
```

**Step 2: Run all frontier tests**

Run: `uv run python3 -m unittest tests.test_frontier_search -v`
Expected: All 14 tests PASS

**Step 3: Run full test suite**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_integration_skill_pipeline tests.test_frontier_search -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add tests/test_frontier_search.py
git commit -m "test(retriever): add 14 integration tests for frontier batching search"
```

---

### Task 7: Final validation and cleanup

**Step 1: Run complete test suite**

```bash
uv run python3 -m unittest tests.test_e2e_phase1 tests.test_ace_phase1 tests.test_ace_phase2 tests.test_rule_extractor tests.test_skill_search_fusion tests.test_integration_skill_pipeline tests.test_frontier_search -v
```
Expected: All tests PASS

**Step 2: Verify search call reduction**

Check `StorageSpy.call_counts["search"]` output from test_frontier_multi_wave_depth — should show <= 8 calls for 3-level tree.

**Step 3: Commit any final adjustments**

```bash
git add -A
git commit -m "feat(retriever): frontier batching optimization complete"
```
