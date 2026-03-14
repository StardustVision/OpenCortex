"""
Tests verifying Phase 2 HTTP endpoints are gated by config.

When archivist_enabled=False (Phase 1 default), knowledge/* and archivist/*
endpoints should return {"error": "feature disabled"}.

Uses same test pattern as test_http_server.py:
  - httpx.AsyncClient + ASGITransport (no JWT auth, no lifespan)
  - http_server._orchestrator = orch (direct injection)
  - http_server._register_routes(app) (routes without middleware)
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from opencortex.config import CortexConfig, init_config
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.orchestrator import MemoryOrchestrator
from tests.test_e2e_phase1 import MockEmbedder, InMemoryStorage


@asynccontextmanager
async def _shrinkage_test_app():
    """Create test app with default config (Phase 2 disabled)."""
    import opencortex.http.server as http_server

    temp_dir = tempfile.mkdtemp(prefix="p2s_test_")
    config = CortexConfig(
        data_root=temp_dir,
        embedding_dimension=MockEmbedder.DIMENSION,
        rerank_provider="disabled",
    )
    init_config(config)

    tokens = set_request_identity("testteam", "alice")
    storage = InMemoryStorage()
    embedder = MockEmbedder()
    orch = MemoryOrchestrator(config=config, storage=storage, embedder=embedder)
    await orch.init()
    http_server._orchestrator = orch

    app = FastAPI()
    http_server._register_routes(app)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        try:
            yield client, orch
        finally:
            await orch.close()
            http_server._orchestrator = None
            reset_request_identity(tokens)
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestPhase2Shrinkage(unittest.TestCase):
    """Verify Phase 2 features are disabled by default."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_default_config_disables_trace_splitter(self):
        """TraceSplitter should not be initialized with default config."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                self.assertIsNone(orch._trace_splitter)
        self._run(_test())

    def test_default_config_disables_archivist(self):
        """Archivist should not be initialized with default config."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                self.assertIsNone(orch._archivist)
        self._run(_test())

    def test_observer_still_enabled(self):
        """Observer should always be initialized (lightweight, needed for transcript)."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                self.assertIsNotNone(orch._observer)
        self._run(_test())

    def test_trace_store_not_initialized_when_disabled(self):
        """TraceStore should not be initialized when trace_splitter disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                self.assertIsNone(orch._trace_store)
        self._run(_test())

    def test_knowledge_store_not_initialized_when_disabled(self):
        """KnowledgeStore should not be initialized when archivist disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                self.assertIsNone(orch._knowledge_store)
        self._run(_test())

    def test_session_end_no_traces_when_disabled(self):
        """session_end should produce traces=0 when TraceSplitter disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                cm = orch._context_manager
                # Run full lifecycle
                await cm.handle(session_id="s1", phase="prepare",
                                tenant_id="testteam", user_id="alice",
                                turn_id="t1",
                                messages=[{"role": "user", "content": "hello"}])
                await cm.handle(session_id="s1", phase="commit",
                                tenant_id="testteam", user_id="alice",
                                turn_id="t1",
                                messages=[{"role": "user", "content": "hello"},
                                          {"role": "assistant", "content": "hi"}])
                result = await cm.handle(session_id="s1", phase="end",
                                         tenant_id="testteam", user_id="alice")
                self.assertEqual(result["traces"], 0)
        self._run(_test())

    def test_knowledge_search_returns_disabled(self):
        """POST /api/v1/knowledge/search returns error when disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                resp = await client.post("/api/v1/knowledge/search",
                                         json={"query": "test", "limit": 5})
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("error", data)
                self.assertEqual(data["error"], "feature disabled")
        self._run(_test())

    def test_knowledge_candidates_returns_disabled(self):
        """GET /api/v1/knowledge/candidates returns error when disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                resp = await client.get("/api/v1/knowledge/candidates")
                data = resp.json()
                self.assertIn("error", data)
                self.assertEqual(data["error"], "feature disabled")
        self._run(_test())

    def test_archivist_trigger_returns_disabled(self):
        """POST /api/v1/archivist/trigger returns error when disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                resp = await client.post("/api/v1/archivist/trigger")
                data = resp.json()
                self.assertIn("error", data)
                self.assertEqual(data["error"], "feature disabled")
        self._run(_test())

    def test_archivist_status_returns_disabled(self):
        """GET /api/v1/archivist/status returns error when disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                resp = await client.get("/api/v1/archivist/status")
                data = resp.json()
                self.assertIn("error", data)
                self.assertEqual(data["error"], "feature disabled")
        self._run(_test())


if __name__ == "__main__":
    unittest.main()
