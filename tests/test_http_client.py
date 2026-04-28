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
