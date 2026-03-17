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

    def test_volcengine_embedder_wrapped_with_cache(self):
        oc = _make_oc("volcengine", "ep-test-model")
        mock_wrap = self._run_with_mocked_provider(
            oc,
            "opencortex.models.embedder.volcengine_embedders",
            "VolcengineDenseEmbedder",
        )
        mock_wrap.assert_called_once()

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
