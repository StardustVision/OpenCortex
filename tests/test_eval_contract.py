"""
Contract tests for OCClient <-> HTTP server protocol.

Uses httpx.AsyncClient + ASGITransport against the FastAPI app (same pattern
as tests/test_http_server.py -- manual orchestrator setup, no JWT middleware).
Verifies that the HTTP API produces correct payloads and the server returns
expected shapes.

No external server or LLM API required.
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from typing import Any, Dict, List
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from httpx import ASGITransport

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator

# Import InMemoryStorage from the existing test helper module
from tests.test_http_server import InMemoryStorage


# =============================================================================
# Mock Embedder (same pattern as tests/test_http_server.py)
# =============================================================================


class MockEmbedder(DenseEmbedderBase):
    DIMENSION = 4

    def __init__(self):
        super().__init__(model_name="mock-embedder-v1")

    def embed(self, text: str) -> EmbedResult:
        return EmbedResult(dense_vector=self._text_to_vector(text))

    def get_dimension(self) -> int:
        return self.DIMENSION

    @staticmethod
    def _text_to_vector(text: str) -> List[float]:
        h = hash(text) & 0xFFFFFFFF
        raw = [
            ((h >> 0) & 0xFF) / 255.0,
            ((h >> 8) & 0xFF) / 255.0,
            ((h >> 16) & 0xFF) / 255.0,
            ((h >> 24) & 0xFF) / 255.0,
        ]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


# =============================================================================
# Test App Factory (same pattern as tests/test_http_server.py)
# =============================================================================


@asynccontextmanager
async def _test_app_context():
    """Create a FastAPI app wired to in-memory test backends.

    Yields an httpx.AsyncClient bound to the ASGI app.
    Manually manages the orchestrator lifecycle since httpx ASGITransport
    does not trigger ASGI lifespan events.
    No JWT middleware is added -- matches test_http_server.py pattern.
    """
    from fastapi import FastAPI
    import opencortex.http.server as http_server

    temp_dir = tempfile.mkdtemp(prefix="eval_contract_")
    config = CortexConfig(
        data_root=temp_dir,
        embedding_dimension=MockEmbedder.DIMENSION,
    )
    init_config(config)

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
            yield client
        finally:
            await orch.close()
            http_server._orchestrator = None
            shutil.rmtree(temp_dir, ignore_errors=True)


# =============================================================================
# Contract Tests
# =============================================================================


class TestEvalContract(unittest.TestCase):
    """Contract tests: HTTP API payload shape verification."""

    def _run(self, coro):
        return asyncio.run(coro)

    # -------------------------------------------------------------------------
    # 1. Store with meta (ingest_mode + source_path)
    # -------------------------------------------------------------------------

    def test_store_with_meta(self):
        """POST /api/v1/memory/store with meta dict produces correct payload."""
        async def _test():
            async with _test_app_context() as client:
                resp = await client.post(
                    "/api/v1/memory/store",
                    json={
                        "abstract": "Test document title",
                        "content": "# Heading\n\nParagraph content here.",
                        "context_type": "resource",
                        "meta": {
                            "ingest_mode": "document",
                            "source_path": "test.md",
                        },
                        "dedup": False,
                    },
                )
                self.assertIn(
                    resp.status_code, (200, 201),
                    f"Store failed: {resp.text}",
                )
                data = resp.json()
                # Response must contain a uri field
                self.assertIn("uri", data)
                # context_type should be reflected back
                self.assertIn("context_type", data)
                # abstract should be reflected back
                self.assertIn("abstract", data)

        self._run(_test())

    # -------------------------------------------------------------------------
    # 2. Search with context_type filter
    # -------------------------------------------------------------------------

    def test_search_with_context_type(self):
        """POST /api/v1/memory/search with context_type filter returns results list."""
        async def _test():
            async with _test_app_context() as client:
                # Store a memory first
                await client.post(
                    "/api/v1/memory/store",
                    json={
                        "abstract": "test memory for search contract",
                        "content": "content body",
                        "context_type": "memory",
                        "dedup": False,
                    },
                )

                # Search with context_type filter
                resp = await client.post(
                    "/api/v1/memory/search",
                    json={
                        "query": "test memory",
                        "limit": 5,
                        "detail_level": "l1",
                        "context_type": "resource",
                    },
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                # Response shape must have results list and total
                self.assertIn("results", data)
                self.assertIsInstance(data["results"], list)
                self.assertIn("total", data)
                self.assertIsInstance(data["total"], int)

        self._run(_test())

    # -------------------------------------------------------------------------
    # 3. Context recall (prepare phase) response shape
    # -------------------------------------------------------------------------

    def test_context_recall_response_shape(self):
        """POST /api/v1/context with phase=prepare returns expected fields."""
        async def _test():
            async with _test_app_context() as client:
                resp = await client.post(
                    "/api/v1/context",
                    json={
                        "session_id": "test-session-contract",
                        "phase": "prepare",
                        "turn_id": "t0",
                        "messages": [{"role": "user", "content": "hello world"}],
                        "config": {"max_items": 5, "detail_level": "l1"},
                    },
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                # Prepare response must include memory list and session_id
                self.assertIn("memory", data)
                self.assertIsInstance(data["memory"], list)
                self.assertIn("session_id", data)
                self.assertEqual(data["session_id"], "test-session-contract")
                # Should also have intent info
                self.assertIn("intent", data)
                self.assertIn("memory_pipeline", data["intent"])
                self.assertIn("runtime", data["intent"]["memory_pipeline"])
                self.assertIn(
                    "stage_timing_ms",
                    data["intent"]["memory_pipeline"]["runtime"]["trace"],
                )
                self.assertIn(
                    "latency_ms",
                    data["intent"]["memory_pipeline"]["runtime"]["trace"],
                )
                self.assertIn(
                    "retrieve",
                    data["intent"]["memory_pipeline"]["runtime"]["trace"][
                        "latency_ms"
                    ],
                )
                self.assertIn(
                    "hydrate",
                    data["intent"]["memory_pipeline"]["runtime"]["trace"][
                        "latency_ms"
                    ]["stages"],
                )

        self._run(_test())


if __name__ == "__main__":
    unittest.main()
