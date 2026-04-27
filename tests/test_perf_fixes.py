# tests/test_perf_fixes.py
import asyncio
import sys
import time
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
        from opencortex.lifecycle.bootstrapper import SubsystemBootstrapper

        async def slow_maintenance(self_inner):
            await asyncio.sleep(60)  # would block init if awaited

        with (
            patch.object(
                SubsystemBootstrapper, "_startup_maintenance", slow_maintenance
            ),
            patch(
                "opencortex.storage.collection_schemas.init_context_collection",
                new_callable=AsyncMock,
            ),
            patch(
                "opencortex.storage.cortex_fs.init_cortex_fs", return_value=MagicMock()
            ),
            patch.object(
                SubsystemBootstrapper, "_create_default_embedder", return_value=None
            ),
            patch.object(
                SubsystemBootstrapper, "_init_cognition", new_callable=AsyncMock
            ),
            patch.object(SubsystemBootstrapper, "_init_alpha", new_callable=AsyncMock),
            patch.object(
                SubsystemBootstrapper, "_init_skill_engine", new_callable=AsyncMock
            ),
        ):
            # Provide storage directly so the lazy QdrantStorageAdapter import is skipped
            oc = MemoryOrchestrator(CortexConfig(), storage=AsyncMock())
            t0 = asyncio.get_event_loop().time()
            await oc.init()
            elapsed = asyncio.get_event_loop().time() - t0

        assert oc._initialized is True
        assert elapsed < 1.0, (
            f"init() took {elapsed:.2f}s — maintenance leaked into init"
        )


class TestProbeNonBlocking(unittest.IsolatedAsyncioTestCase):
    async def test_probe_embedding_runs_off_event_loop(self):
        from opencortex.intent import MemoryBootstrapProbe, ProbeScopeSource, ScopeLevel

        class SlowEmbedder:
            is_available = True
            model_name = "slow-probe-embedder"

            def embed_query(self, query: str):
                time.sleep(0.05)
                return MagicMock(dense_vector=[0.1, 0.2, 0.3])

        probe = MemoryBootstrapProbe(
            storage=MagicMock(),
            embedder=SlowEmbedder(),
            collection_resolver=lambda: "context",
            filter_builder=lambda: {},
        )
        probe._select_scope_bucket = AsyncMock(
            return_value=(
                None,
                ProbeScopeSource.GLOBAL_ROOT,
                False,
                ScopeLevel.GLOBAL,
                [],
                [],
                0.0,
            )
        )
        probe._object_probe = AsyncMock(return_value=[])
        probe._anchor_probe = AsyncMock(return_value=[])

        ticks = 0
        done = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not done.is_set():
                ticks += 1
                await asyncio.sleep(0.005)

        ticker_task = asyncio.create_task(ticker())
        try:
            result = await probe.probe("What happened before launch?")
            self.assertIsNotNone(result)
        finally:
            done.set()
            await ticker_task

        self.assertGreaterEqual(ticks, 5)


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
            if (
                OwnerType.MEMORY in seen_owner_types
                and OwnerType.TRACE in seen_owner_types
            ):
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

        from opencortex.lifecycle.background_tasks import BackgroundTaskManager
        from opencortex.lifecycle.bootstrapper import SubsystemBootstrapper

        async def fake_init_cognition(self_inner):
            # Ensure _start_autophagy_sweeper() doesn't early-return.
            self_inner._orch._autophagy_kernel = MagicMock()

        async def slow_startup_sweep(self_inner):
            await asyncio.sleep(60)

        async def slow_periodic_loop(self_inner):
            await asyncio.sleep(60)

        with (
            patch.object(SubsystemBootstrapper, "_init_cognition", fake_init_cognition),
            patch.object(
                BackgroundTaskManager, "_run_autophagy_sweep_once", slow_startup_sweep
            ),
            patch.object(
                BackgroundTaskManager, "_autophagy_sweep_loop", slow_periodic_loop
            ),
            patch(
                "opencortex.storage.collection_schemas.init_context_collection",
                new_callable=AsyncMock,
            ),
            patch(
                "opencortex.storage.cortex_fs.init_cortex_fs", return_value=MagicMock()
            ),
            patch.object(
                SubsystemBootstrapper, "_create_default_embedder", return_value=None
            ),
            patch.object(SubsystemBootstrapper, "_init_alpha", new_callable=AsyncMock),
            patch.object(
                SubsystemBootstrapper, "_init_skill_engine", new_callable=AsyncMock
            ),
        ):
            oc = MemoryOrchestrator(CortexConfig(), storage=AsyncMock())
            t0 = asyncio.get_event_loop().time()
            await oc.init()
            elapsed = asyncio.get_event_loop().time() - t0

            assert elapsed < 1.0, (
                f"init() took {elapsed:.2f}s — autophagy leaked into init"
            )
            assert oc._autophagy_startup_sweep_task is not None
            assert oc._autophagy_sweep_task is not None
            assert oc._autophagy_startup_sweep_task.done() is False
            assert oc._autophagy_sweep_task.done() is False

            await oc.close()

    async def test_init_skips_cognition_when_disabled(self):
        """init() should not initialize cognition or start sweeper when disabled."""
        from opencortex.config import CortexConfig
        from opencortex.orchestrator import MemoryOrchestrator

        with (
            patch.object(
                MemoryOrchestrator,
                "_init_cognition",
                new_callable=AsyncMock,
            ) as mock_init_cognition,
            patch.object(
                MemoryOrchestrator,
                "_start_autophagy_sweeper",
            ) as mock_start_sweeper,
            patch(
                "opencortex.storage.collection_schemas.init_context_collection",
                new_callable=AsyncMock,
            ),
            patch(
                "opencortex.storage.cortex_fs.init_cortex_fs",
                return_value=MagicMock(),
            ),
            patch.object(
                MemoryOrchestrator,
                "_create_default_embedder",
                return_value=None,
            ),
            patch.object(
                MemoryOrchestrator,
                "_init_alpha",
                new_callable=AsyncMock,
            ),
            patch.object(
                MemoryOrchestrator,
                "_init_skill_engine",
                new_callable=AsyncMock,
            ),
        ):
            oc = MemoryOrchestrator(
                CortexConfig(cognition_enabled=False),
                storage=AsyncMock(),
            )
            await oc.init()

            mock_init_cognition.assert_not_awaited()
            mock_start_sweeper.assert_not_called()
            self.assertIsNone(oc._autophagy_kernel)

            await oc.close()

    async def test_sweep_is_serialized_across_overlapping_calls(self):
        """Overlapping sweep triggers must not run concurrently (shared cursor state)."""
        import asyncio

        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig

        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._config = CortexConfig(
            autophagy_sweep_interval_seconds=0.01, autophagy_sweep_batch_size=2
        )
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
        assert max_in_flight == 1, (
            f"expected serialized sweeps; saw concurrency={max_in_flight}"
        )

    async def test_sweep_failure_is_isolated_per_owner_type(self):
        """If MEMORY sweep fails, TRACE sweep should still run for that tick."""
        import asyncio

        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig
        from opencortex.cognition.state_types import OwnerType

        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._config = CortexConfig(
            autophagy_sweep_interval_seconds=0.01, autophagy_sweep_batch_size=2
        )
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


class TestRecallBookkeepingAsync(unittest.IsolatedAsyncioTestCase):
    async def test_search_skips_recall_bookkeeping_side_effects(self):
        """search() should skip recall bookkeeping side effects on the hot path."""
        from opencortex.config import CortexConfig
        from opencortex.intent import RetrievalDepth, SearchResult
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.retrieve.types import ContextType, FindResult, MatchedContext

        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._config = CortexConfig()
        oc._initialized = True
        oc._context_manager = None
        oc._storage = MagicMock()
        oc._storage.close = AsyncMock()
        oc._autophagy_startup_sweep_task = None
        oc._autophagy_sweep_task = None
        oc._skill_manager = None
        oc._memory_runtime = MagicMock()
        oc._memory_runtime.arbitrate_hydration.return_value = (
            {
                "memory_limit": 1,
                "effective_depth": "l0",
            },
            False,
            [],
            False,
        )
        oc._memory_runtime.finalize.return_value = MagicMock()
        oc._build_typed_queries = MagicMock(return_value=[MagicMock()])
        oc._summarize_retrieve_breakdown = MagicMock(
            return_value={
                "embed": 1.0,
                "search": 1.0,
                "rerank": 0.0,
                "assemble": 0.0,
                "total": 2.0,
            }
        )
        oc.bind_memory_runtime = MagicMock(
            return_value={
                "memory_limit": 1,
                "effective_depth": "l0",
            }
        )

        async def fake_query(**_kwargs):
            return MagicMock(
                timing_ms={
                    "embed": 1.0,
                    "search": 1.0,
                    "rerank": 0.0,
                    "assemble": 0.0,
                    "total": 2.0,
                },
                explain=MagicMock(rerank_ms=0),
                matched_contexts=[],
            )

        oc._execute_object_query = AsyncMock(side_effect=fake_query)
        oc._resolve_and_update_access_stats = AsyncMock()
        memory = MatchedContext(
            uri="opencortex://tenant/user/memories/test",
            context_type=ContextType.MEMORY,
            is_leaf=True,
            abstract="test memory",
        )
        oc._aggregate_results = MagicMock(
            return_value=FindResult(memories=[memory], resources=[], skills=[])
        )
        oc._autophagy_kernel = MagicMock()
        oc._autophagy_kernel.apply_recall_outcome = AsyncMock()

        probe_result = SearchResult(should_recall=True)
        retrieve_plan = MagicMock(retrieval_depth=RetrievalDepth.L0)

        t0 = asyncio.get_running_loop().time()
        result = await oc.search(
            query="test question",
            probe_result=probe_result,
            retrieve_plan=retrieve_plan,
        )
        elapsed = asyncio.get_running_loop().time() - t0

        self.assertEqual(len(result.memories), 1)
        self.assertLess(
            elapsed,
            0.5,
            f"search() blocked on recall bookkeeping for {elapsed:.3f}s",
        )
        await oc.close()
        oc._resolve_and_update_access_stats.assert_not_awaited()
        oc._autophagy_kernel.apply_recall_outcome.assert_not_awaited()
        self.assertEqual(len(oc._recall_bookkeeping_tasks_set()), 0)

    async def test_search_no_longer_calls_hyde_rewrite(self):
        """search() should not invoke the old retrieval-time HyDE callback."""
        from opencortex.config import CortexConfig
        from opencortex.intent import RetrievalDepth, SearchResult
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.retrieve.types import ContextType, FindResult, MatchedContext

        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._config = CortexConfig()
        oc._initialized = True
        oc._context_manager = None
        oc._storage = MagicMock()
        oc._storage.close = AsyncMock()
        oc._autophagy_startup_sweep_task = None
        oc._autophagy_sweep_task = None
        oc._autophagy_kernel = None
        oc._skill_manager = None
        oc._llm_completion = AsyncMock(
            side_effect=AssertionError("unexpected HyDE call")
        )
        oc._memory_runtime = MagicMock()
        oc._memory_runtime.arbitrate_hydration.return_value = (
            {
                "memory_limit": 1,
                "effective_depth": "l0",
            },
            False,
            [],
            False,
        )
        oc._memory_runtime.finalize.return_value = MagicMock()
        oc._build_typed_queries = MagicMock(
            return_value=[
                MagicMock(
                    query="test question",
                    target_directories=[],
                )
            ]
        )
        oc._summarize_retrieve_breakdown = MagicMock(
            return_value={
                "embed": 1.0,
                "search": 1.0,
                "rerank": 0.0,
                "assemble": 0.0,
                "total": 2.0,
            }
        )
        oc.bind_memory_runtime = MagicMock(
            return_value={
                "memory_limit": 1,
                "effective_depth": "l0",
            }
        )
        oc._execute_object_query = AsyncMock(
            return_value=MagicMock(
                timing_ms={
                    "embed": 1.0,
                    "search": 1.0,
                    "rerank": 0.0,
                    "assemble": 0.0,
                    "total": 2.0,
                },
                explain=MagicMock(rerank_ms=0),
                matched_contexts=[],
            )
        )
        oc._resolve_and_update_access_stats = AsyncMock()
        memory = MatchedContext(
            uri="opencortex://tenant/user/memories/test",
            context_type=ContextType.MEMORY,
            is_leaf=True,
            abstract="test memory",
        )
        oc._aggregate_results = MagicMock(
            return_value=FindResult(memories=[memory], resources=[], skills=[])
        )

        result = await oc.search(
            query="test question",
            probe_result=SearchResult(should_recall=True),
            retrieve_plan=MagicMock(retrieval_depth=RetrievalDepth.L0),
        )

        self.assertEqual(len(result.memories), 1)
        oc._llm_completion.assert_not_awaited()


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

        items = [
            {"content": f"doc {i}", "meta": {"file_path": f"f{i}.txt"}}
            for i in range(8)
        ]
        await oc.batch_add(items)

        assert concurrent_high_water >= 2, (
            f"Expected ≥2 concurrent items, got max {concurrent_high_water}"
        )

    async def test_batch_add_creates_work_in_bounded_chunks(self):
        """batch_add must not create tasks for the entire request at once."""
        import opencortex.services.memory_service as memory_service
        from opencortex.config import CortexConfig
        from opencortex.orchestrator import MemoryOrchestrator

        old_concurrency = memory_service._BATCH_ADD_CONCURRENCY
        old_chunk_size = memory_service._BATCH_ADD_TASK_CHUNK_SIZE
        memory_service._BATCH_ADD_CONCURRENCY = 100
        memory_service._BATCH_ADD_TASK_CHUNK_SIZE = 3

        started: list[str] = []
        release = asyncio.Event()

        async def fake_gen_abstract(content, file_path):
            started.append(file_path)
            await release.wait()
            return "abstract", "overview"

        async def fake_add(**kwargs):
            m = MagicMock()
            m.uri = f"opencortex://t/u/mem/ev/{len(started)}"
            return m

        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._initialized = True
        oc._config = CortexConfig()
        oc._ensure_init = lambda: None
        oc._generate_abstract_overview = fake_gen_abstract
        oc.add = fake_add

        items = [
            {"content": f"doc {i}", "meta": {"file_path": f"f{i}.txt"}}
            for i in range(7)
        ]
        try:
            batch_task = asyncio.create_task(oc.batch_add(items))
            while len(started) < 3:
                await asyncio.sleep(0)
            await asyncio.sleep(0.01)
            self.assertEqual(started, ["f0.txt", "f1.txt", "f2.txt"])
            release.set()
            result = await batch_task
            self.assertEqual(result["imported"], 7)
        finally:
            memory_service._BATCH_ADD_CONCURRENCY = old_concurrency
            memory_service._BATCH_ADD_TASK_CHUNK_SIZE = old_chunk_size
