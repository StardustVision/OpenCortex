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
        from opencortex.cognition.state_types import OwnerType

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
        seen_owner_types = []

        async def sweep_metabolism(**kwargs):
            seen_owner_types.append(kwargs.get("owner_type"))
            if OwnerType.MEMORY in seen_owner_types and OwnerType.TRACE in seen_owner_types:
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
        assert OwnerType.MEMORY in seen_owner_types
        assert OwnerType.TRACE in seen_owner_types

    async def test_init_starts_autophagy_sweeper_fire_and_forget(self):
        """init() should schedule autophagy sweeps without awaiting them."""
        import asyncio

        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig

        async def fake_init_cognition(self_inner):
            # Ensure _start_autophagy_sweeper() doesn't early-return.
            self_inner._autophagy_kernel = MagicMock()

        async def slow_startup_sweep(self_inner):
            await asyncio.sleep(60)

        async def slow_periodic_loop(self_inner):
            await asyncio.sleep(60)

        with patch.object(MemoryOrchestrator, "_init_cognition", fake_init_cognition), \
             patch.object(MemoryOrchestrator, "_run_autophagy_sweep_once", slow_startup_sweep), \
             patch.object(MemoryOrchestrator, "_autophagy_sweep_loop", slow_periodic_loop), \
             patch("opencortex.orchestrator.init_context_collection", new_callable=AsyncMock), \
             patch("opencortex.orchestrator.init_cortex_fs", return_value=MagicMock()), \
             patch.object(MemoryOrchestrator, "_create_default_embedder", return_value=None), \
             patch.object(MemoryOrchestrator, "_init_alpha", new_callable=AsyncMock), \
             patch.object(MemoryOrchestrator, "_init_skill_engine", new_callable=AsyncMock):

            oc = MemoryOrchestrator(CortexConfig(), storage=AsyncMock())
            t0 = asyncio.get_event_loop().time()
            await oc.init()
            elapsed = asyncio.get_event_loop().time() - t0

            assert elapsed < 1.0, f"init() took {elapsed:.2f}s — autophagy leaked into init"
            assert oc._autophagy_startup_sweep_task is not None
            assert oc._autophagy_sweep_task is not None
            assert oc._autophagy_startup_sweep_task.done() is False
            assert oc._autophagy_sweep_task.done() is False

            await oc.close()

    async def test_sweep_is_serialized_across_overlapping_calls(self):
        """Overlapping sweep triggers must not run concurrently (shared cursor state)."""
        import asyncio

        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig

        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._config = CortexConfig(autophagy_sweep_interval_seconds=0.01, autophagy_sweep_batch_size=2)
        oc._storage = MagicMock()
        oc._context_manager = None
        oc._initialized = True

        in_flight = 0
        max_in_flight = 0
        first_enter = asyncio.Event()
        allow_exit = asyncio.Event()

        async def sweep_metabolism(**kwargs):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if not first_enter.is_set():
                first_enter.set()
                await allow_exit.wait()
            in_flight -= 1
            return MagicMock(next_cursor=None)

        oc._autophagy_kernel = MagicMock()
        oc._autophagy_kernel.sweep_metabolism = AsyncMock(side_effect=sweep_metabolism)

        t1 = asyncio.create_task(oc._run_autophagy_sweep_once())
        await asyncio.wait_for(first_enter.wait(), timeout=1.0)
        t2 = asyncio.create_task(oc._run_autophagy_sweep_once())
        await asyncio.sleep(0.01)  # give the second sweep a chance to attempt entry
        allow_exit.set()

        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)
        assert max_in_flight == 1, f"expected serialized sweeps; saw concurrency={max_in_flight}"

    async def test_sweep_failure_is_isolated_per_owner_type(self):
        """If MEMORY sweep fails, TRACE sweep should still run for that tick."""
        import asyncio

        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig
        from opencortex.cognition.state_types import OwnerType

        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._config = CortexConfig(autophagy_sweep_interval_seconds=0.01, autophagy_sweep_batch_size=2)
        oc._storage = MagicMock()
        oc._context_manager = None
        oc._initialized = True

        trace_called = asyncio.Event()

        async def sweep_metabolism(**kwargs):
            ot = kwargs.get("owner_type")
            if ot == OwnerType.MEMORY:
                raise RuntimeError("boom")
            if ot == OwnerType.TRACE:
                trace_called.set()
            return MagicMock(next_cursor=None)

        oc._autophagy_kernel = MagicMock()
        oc._autophagy_kernel.sweep_metabolism = AsyncMock(side_effect=sweep_metabolism)

        await oc._run_autophagy_sweep_once()
        await asyncio.wait_for(trace_called.wait(), timeout=1.0)




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
