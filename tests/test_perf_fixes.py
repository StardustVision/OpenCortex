# tests/test_perf_fixes.py
import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_oc(provider: str, model: str = "text-embedding-3-small"):
    """Return a MemoryOrchestrator instance bypassing __init__ for unit tests."""
    from opencortex.orchestrator import MemoryOrchestrator
    from opencortex.config import CortexConfig
    oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
    oc._config = CortexConfig(
        embedding_provider=provider,
        embedding_model=model,
        embedding_api_key="test-key",
    )
    return oc


class TestEmbedderCache(unittest.TestCase):
    def _run_with_mocked_provider(self, oc, module_name: str, class_name: str):
        """
        Inject a mock module for `module_name` and spy on _wrap_with_cache.
        Returns the MagicMock assigned to `oc._wrap_with_cache`.
        """
        mock_wrap = MagicMock(side_effect=lambda e: e)
        oc._wrap_with_cache = mock_wrap  # instance override shadows class method

        mock_mod = MagicMock()
        setattr(mock_mod, class_name, MagicMock(return_value=MagicMock()))
        with patch.dict("sys.modules", {module_name: mock_mod}):
            oc._create_default_embedder()
        return mock_wrap

    def test_openai_embedder_wrapped_with_cache(self):
        oc = _make_oc("openai")
        mock_wrap = self._run_with_mocked_provider(
            oc,
            "opencortex.models.embedder.openai_embedder",
            "OpenAIDenseEmbedder",
        )
        mock_wrap.assert_called_once()


class TestAccessStatsNoAmplification(unittest.IsolatedAsyncioTestCase):
    async def test_single_filter_no_get_parallel_update(self):
        """One filter() call, zero get() calls, N parallel update() calls."""
        from opencortex.orchestrator import MemoryOrchestrator, _CONTEXT_COLLECTION
        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._storage = AsyncMock()
        oc._storage.filter.return_value = [
            {"id": "id1", "uri": "uri1", "active_count": 3},
            {"id": "id2", "uri": "uri2", "active_count": 7},
        ]

        await oc._resolve_and_update_access_stats(["uri1", "uri2"])

        # Exactly one filter call covering all URIs at once
        oc._storage.filter.assert_called_once()
        call_filter = oc._storage.filter.call_args[0][1]  # second positional arg
        assert set(call_filter["conds"]) == {"uri1", "uri2"}

        # Zero get() calls — active_count comes from filter payload
        oc._storage.get.assert_not_called()

        # Two update() calls, incremented counts
        assert oc._storage.update.call_count == 2
        calls = {c[0][1]: c[0][2] for c in oc._storage.update.call_args_list}
        assert calls["id1"]["active_count"] == 4
        assert calls["id2"]["active_count"] == 8


class TestColdStartNonBlocking(unittest.IsolatedAsyncioTestCase):
    async def test_init_does_not_await_maintenance(self):
        """init() must complete in under 1 second even if maintenance is slow."""
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig

        async def slow_maintenance(self_inner):
            await asyncio.sleep(60)  # would block init if awaited

        with patch.object(MemoryOrchestrator, "_startup_maintenance", slow_maintenance), \
             patch("opencortex.orchestrator.init_context_collection", new_callable=AsyncMock), \
             patch("opencortex.orchestrator.init_cortex_fs", return_value=MagicMock()), \
             patch("opencortex.orchestrator.HierarchicalRetriever", return_value=MagicMock()), \
             patch.object(MemoryOrchestrator, "_create_default_embedder", return_value=None), \
             patch.object(MemoryOrchestrator, "_init_alpha", new_callable=AsyncMock):

            # Provide storage directly so the lazy QdrantStorageAdapter import is skipped
            oc = MemoryOrchestrator(CortexConfig(), storage=AsyncMock())
            t0 = asyncio.get_event_loop().time()
            await oc.init()
            elapsed = asyncio.get_event_loop().time() - t0

        assert oc._initialized is True
        assert elapsed < 1.0, f"init() took {elapsed:.2f}s — maintenance leaked into init"


class TestAutophagySweeperLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_autophagy_sweeper_invokes_kernel_and_close_cleans_up(self):
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig

        cfg = CortexConfig(
            autophagy_sweep_interval_seconds=0.01,
            autophagy_sweep_batch_size=2,
        )
        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._config = cfg
        oc._context_manager = None
        oc._storage = MagicMock()
        oc._storage.close = AsyncMock()
        oc._initialized = True

        sweep_called = asyncio.Event()

        async def sweep_metabolism(**kwargs):
            sweep_called.set()
            return MagicMock(next_cursor=None)

        oc._autophagy_kernel = MagicMock()
        oc._autophagy_kernel.sweep_metabolism = AsyncMock(side_effect=sweep_metabolism)

        # Expected to exist after Task 7 wiring.
        oc._start_autophagy_sweeper()

        try:
            await asyncio.wait_for(sweep_called.wait(), timeout=1.0)
            assert oc._autophagy_kernel.sweep_metabolism.await_count >= 1
        finally:
            await oc.close()

        assert oc._autophagy_sweep_task is None or oc._autophagy_sweep_task.done()


class TestFrontierStillStarvedBatch(unittest.IsolatedAsyncioTestCase):
    async def test_still_starved_single_batch_query(self):
        """3 still-starved parents → 3 searches total (main+comp+batch), not 5."""
        from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever

        call_count = 0

        async def fake_search(**kwargs):
            nonlocal call_count
            call_count += 1
            f = kwargs.get("filter", {})
            conds = f.get("conds", [])
            # On the 3rd call (still-starved batch), return one child for parent_a
            if call_count == 3 and "parent_a" in conds:
                return [{"uri": "child1", "parent_uri": "parent_a", "_score": 0.9,
                         "is_leaf": True, "abstract": "x", "active_count": 0,
                         "category": "", "keywords": ""}]
            return []

        storage = MagicMock()
        storage.search = fake_search

        hr = HierarchicalRetriever(
            storage=storage,
            embedder=None,
            rerank_config=None,
            llm_completion=None,
            max_waves=1,
        )

        starting_points = [("parent_a", 0.9), ("parent_b", 0.8), ("parent_c", 0.7)]
        await hr._frontier_search_impl(
            query="test",
            collection="context",
            query_vector=[0.1] * 10,
            sparse_query_vector=None,
            starting_points=starting_points,
            limit=5,
            mode="wave",
            threshold=None,
            metadata_filter=None,
            text_query="",
        )

        # 1 main wave + 1 compensation + 1 still-starved batch = 3, NOT 5
        assert call_count == 3, f"Expected 3 search calls, got {call_count}"


class TestResultAssemblyBatchedRelations(unittest.IsolatedAsyncioTestCase):
    async def test_relations_read_in_two_batches_not_n(self):
        """5 candidates → 5 get_relations + 1 read_batch total, not 5 read_batch."""
        from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
        from opencortex.retrieve.types import ContextType, DetailLevel

        read_batch_calls = []

        async def fake_get_relations(uri):
            return [f"rel_{uri}"]

        async def fake_read_batch(uris, level="l0"):
            read_batch_calls.append(sorted(uris))
            return {u: f"abstract_{u}" for u in uris}

        mock_fs = MagicMock()
        mock_fs.get_relations = fake_get_relations
        mock_fs.read_batch = fake_read_batch

        with patch(
            "opencortex.retrieve.hierarchical_retriever._get_cortex_fs",
            return_value=mock_fs,
        ):
            hr = HierarchicalRetriever(
                storage=MagicMock(), embedder=None,
                rerank_config=None, llm_completion=None,
            )
            candidates = [
                {"uri": f"oc://t/u/mem/c/n{i}", "abstract": f"a{i}",
                 "overview": "", "is_leaf": True, "context_type": "memory",
                 "category": "events", "keywords": "", "_final_score": 0.9}
                for i in range(5)
            ]
            await hr._convert_to_matched_contexts(
                candidates, ContextType.MEMORY, DetailLevel.L1,
            )

        # read_batch called exactly once (not 5 times)
        assert len(read_batch_calls) == 1, (
            f"Expected 1 read_batch call, got {len(read_batch_calls)}"
        )
        assert len(read_batch_calls[0]) == 5  # 5 unique related URIs in one call


class TestBatchAddConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_items_processed_concurrently(self):
        """batch_add with 8 items must run at least 2 concurrently."""
        from opencortex.orchestrator import MemoryOrchestrator

        concurrent_high_water = 0
        in_flight = 0

        async def fake_gen_abstract(content, file_path):
            nonlocal concurrent_high_water, in_flight
            in_flight += 1
            concurrent_high_water = max(concurrent_high_water, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return "abstract", "overview"

        async def fake_add(**kwargs):
            m = MagicMock()
            m.uri = "opencortex://t/u/mem/ev/test"
            return m

        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._initialized = True
        oc._ensure_init = lambda: None
        oc._generate_abstract_overview = fake_gen_abstract
        oc.add = fake_add

        items = [{"content": f"doc {i}", "meta": {"file_path": f"f{i}.txt"}}
                 for i in range(8)]
        await oc.batch_add(items)

        assert concurrent_high_water >= 2, (
            f"Expected ≥2 concurrent items, got max {concurrent_high_water}"
        )
