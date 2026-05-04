# SPDX-License-Identifier: Apache-2.0
"""Integration tests for store/recall lifecycle signal boundaries."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from opencortex.config import CortexConfig, init_config
from opencortex.http.request_context import reset_request_identity, set_request_identity
from opencortex.intent import RetrievalDepth, SearchResult
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.types import ContextType, FindResult, MatchedContext
from opencortex.services.memory_signals import (
    MemorySignalBus,
    MemoryStoredSignal,
    RecallCompletedSignal,
)


class MockEmbedder(DenseEmbedderBase):
    """Small deterministic embedder for signal integration tests."""

    def __init__(self) -> None:
        super().__init__(model_name="mock")

    def embed(self, _text: str) -> EmbedResult:
        return EmbedResult(dense_vector=[0.1, 0.2, 0.3, 0.4])

    def get_dimension(self) -> int:
        return 4


class TestStoreSignals(unittest.IsolatedAsyncioTestCase):
    """Memory store publishes lifecycle signals without plugin coupling."""

    async def test_store_publishes_memory_stored_signal(self) -> None:
        """A successful memory add emits one memory_stored signal."""
        tmpdir = tempfile.mkdtemp()
        cfg = CortexConfig(
            data_root=tmpdir,
            embedding_dimension=4,
            cognition_enabled=False,
            autophagy_plugin_enabled=False,
            skill_engine_enabled=False,
        )
        init_config(cfg)
        orch = MemoryOrchestrator(config=cfg, embedder=MockEmbedder())
        await orch.init()
        received: list[MemoryStoredSignal] = []
        delivered = asyncio.Event()

        async def on_memory_stored(signal: MemoryStoredSignal) -> None:
            received.append(signal)
            delivered.set()

        orch._memory_signal_bus.subscribe("memory_stored", on_memory_stored)
        tokens = set_request_identity("tenant", "user")
        try:
            result = await orch.add(
                abstract="User prefers focused memory tests",
                category="preferences",
                context_type="memory",
            )
            await asyncio.wait_for(delivered.wait(), timeout=1)
        finally:
            reset_request_identity(tokens)
            await orch.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].uri, result.uri)
        self.assertEqual(received[0].tenant_id, "tenant")
        self.assertEqual(received[0].user_id, "user")
        self.assertEqual(received[0].context_type, "memory")


class TestRecallSignals(unittest.IsolatedAsyncioTestCase):
    """Memory search publishes recall signals and skips skill plugin search."""

    async def test_search_publishes_recall_completed_without_skill_lookup(self) -> None:
        """Core recall emits a signal but does not call _skill_manager.search."""
        oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
        oc._config = CortexConfig()
        oc._initialized = True
        oc._memory_signal_bus = MemorySignalBus()
        oc._context_manager = None
        oc._storage = MagicMock()
        oc._storage.close = AsyncMock()
        oc._autophagy_startup_sweep_task = None
        oc._autophagy_sweep_task = None
        oc._skill_manager = MagicMock()
        oc._skill_manager.search = AsyncMock(
            side_effect=AssertionError("skill search should not run")
        )
        oc._memory_runtime = MagicMock()
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
            return_value={"memory_limit": 1, "effective_depth": "l0"}
        )

        async def fake_query(**_kwargs):
            explain = MagicMock()
            explain.rerank_ms = 0
            explain.query_class = "simple_recall"
            explain.path = "vector"
            explain.doc_scope_hit = False
            explain.time_filter_hit = False
            return MagicMock(
                timing_ms={
                    "embed": 1.0,
                    "search": 1.0,
                    "rerank": 0.0,
                    "assemble": 0.0,
                    "total": 2.0,
                },
                explain=explain,
                matched_contexts=[],
            )

        memory = MatchedContext(
            uri="opencortex://tenant/user/memories/test",
            context_type=ContextType.MEMORY,
            is_leaf=True,
            abstract="test memory",
        )
        oc._retrieval_service_instance = SimpleNamespace(
            _build_search_filter=MagicMock(return_value={"op": "and", "conds": []}),
            _execute_object_query=AsyncMock(side_effect=fake_query),
            _aggregate_results=MagicMock(
                return_value=FindResult(memories=[memory], resources=[], skills=[])
            ),
        )

        received: list[RecallCompletedSignal] = []
        delivered = asyncio.Event()

        async def on_recall_completed(signal: RecallCompletedSignal) -> None:
            received.append(signal)
            delivered.set()

        oc._memory_signal_bus.subscribe("recall_completed", on_recall_completed)

        result = await oc.search(
            query="test question",
            probe_result=SearchResult(should_recall=True),
            retrieve_plan=MagicMock(retrieval_depth=RetrievalDepth.L0),
        )
        await asyncio.wait_for(delivered.wait(), timeout=1)

        self.assertEqual(len(result.memories), 1)
        self.assertEqual(result.skills, [])
        oc._skill_manager.search.assert_not_awaited()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].query, "test question")
        self.assertEqual(received[0].memories, [memory])
        await oc.close()


if __name__ == "__main__":
    unittest.main()
