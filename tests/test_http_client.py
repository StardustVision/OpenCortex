"""HTTP client contract tests for typed `memory/search` parsing."""

import unittest

import httpx

from opencortex.http.client import OpenCortexClient, OpenCortexClientError


class TestOpenCortexClient(unittest.IsolatedAsyncioTestCase):
    """Validate client-side parsing for the search transport contract."""

    async def asyncTearDown(self) -> None:
        """Close the underlying client between tests."""
        if hasattr(self, "client"):
            await self.client.close()

    async def test_memory_search_validates_and_returns_compatible_dict(self) -> None:
        """Client should validate search payloads before returning them."""

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v1/memory/search")
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "uri": "/memories/preferences/theme",
                            "abstract": "User prefers dark theme",
                            "context_type": "memory",
                            "score": 0.93,
                            "overview": "Theme preference",
                        }
                    ],
                    "total": 1,
                    "memory_pipeline": {
                        "probe": {
                            "should_recall": True,
                            "scope_source": "global_root",
                        },
                        "planner": {"retrieval_depth": "l1"},
                        "runtime": {"trace": {"hydration": []}},
                    },
                },
            )

        self.client = OpenCortexClient(base_url="http://testserver")
        self.client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        result = await self.client.memory_search("theme")

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["results"][0]["uri"], "/memories/preferences/theme")
        self.assertIn("memory_pipeline", result)
        self.assertIn("runtime", result["memory_pipeline"])

    async def test_memory_search_raises_on_invalid_payload(self) -> None:
        """Client should fail loudly when the response breaks contract."""

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v1/memory/search")
            return httpx.Response(
                200,
                json={
                    "results": "not-a-list",
                    "total": "bad-total",
                },
            )

        self.client = OpenCortexClient(base_url="http://testserver")
        self.client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        with self.assertRaises(OpenCortexClientError):
            await self.client.memory_search("theme")

    async def test_context_prepare_validates_and_returns_compatible_dict(self) -> None:
        """Client should validate the context prepare response payload."""

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/v1/context")
            return httpx.Response(
                200,
                json={
                    "session_id": "sess_ctx_01",
                    "turn_id": "turn_01",
                    "intent": {
                        "should_recall": True,
                        "probe_candidate_count": 1,
                        "probe_top_score": 0.9,
                        "depth": "l1",
                        "memory_pipeline": {
                            "probe": {"should_recall": True},
                            "planner": {"retrieval_depth": "l1"},
                            "runtime": {"trace": {"hydration": []}},
                        },
                    },
                    "memory": [
                        {
                            "uri": "/memories/preferences/theme",
                            "abstract": "User prefers dark theme",
                            "score": 0.93,
                            "context_type": "memory",
                            "category": "preferences",
                        }
                    ],
                    "knowledge": [],
                    "instructions": {
                        "should_cite_memory": True,
                        "memory_confidence": 0.93,
                        "recall_count": 1,
                        "guidance": "Use recalled context.",
                    },
                },
            )

        self.client = OpenCortexClient(base_url="http://testserver")
        self.client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        result = await self.client.context_prepare(
            session_id="sess_ctx_01",
            turn_id="turn_01",
            messages=[{"role": "user", "content": "What theme do I prefer?"}],
        )

        self.assertEqual(result["session_id"], "sess_ctx_01")
        self.assertEqual(result["instructions"]["recall_count"], 1)
        self.assertIn("memory_pipeline", result["intent"])
