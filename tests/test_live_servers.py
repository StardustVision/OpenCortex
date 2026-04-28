"""
Live server regression tests for OpenCortex.

Tests against a RUNNING HTTP Server (port 8921). The server must be started
before running these tests with OPENCORTEX_RUN_LIVE_TESTS=1:

    PYTHONPATH=src uv run python -m opencortex.http --config opencortex.json --port 8921

Run:
    OPENCORTEX_RUN_LIVE_TESTS=1 uv run pytest tests/test_live_servers.py -q
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx

HTTP_BASE = "http://127.0.0.1:8921"
RUN_LIVE_TESTS = os.getenv("OPENCORTEX_RUN_LIVE_TESTS") == "1"


def _http_ok() -> bool:
    """Check if HTTP server is reachable."""
    try:
        r = httpx.get(f"{HTTP_BASE}/api/v1/memory/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


_HTTP_LIVE = RUN_LIVE_TESTS and _http_ok()


# =========================================================================
# Part 1: HTTP Server live tests
# =========================================================================


@unittest.skipUnless(
    _HTTP_LIVE,
    "Live HTTP tests require OPENCORTEX_RUN_LIVE_TESTS=1 and "
    f"a compatible server at {HTTP_BASE}",
)
class TestHTTPLive(unittest.TestCase):
    """Live tests against the running HTTP Server."""

    def _run(self, coro):
        return asyncio.run(coro)

    # ------------------------------------------------------------------
    # 1. Health
    # ------------------------------------------------------------------
    def test_01_health(self):
        """GET /api/v1/memory/health returns initialized=True."""

        async def check():
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=10.0) as c:
                r = await c.get("/api/v1/memory/health")
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertTrue(data["initialized"])
                self.assertTrue(data["storage"])
                self.assertTrue(data["embedder"])

        self._run(check())

    # ------------------------------------------------------------------
    # 2. Store
    # ------------------------------------------------------------------
    def test_02_store(self):
        """POST /api/v1/memory/store creates a memory with Qdrant backend."""

        async def check():
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=30.0) as c:
                r = await c.post(
                    "/api/v1/memory/store",
                    json={
                        "abstract": "Live test: user prefers dark theme in all editors",
                        "category": "live_preferences",
                    },
                )
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertIn("uri", data)
                self.assertEqual(data["context_type"], "memory")

        self._run(check())

    # ------------------------------------------------------------------
    # 3. Search
    # ------------------------------------------------------------------
    def test_03_search(self):
        """POST /api/v1/memory/search returns results from Qdrant."""

        async def check():
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=30.0) as c:
                # Store first
                await c.post(
                    "/api/v1/memory/store",
                    json={
                        "abstract": "Live test: team uses Redis for caching",
                        "category": "live_tech",
                    },
                )
                # Search
                r = await c.post(
                    "/api/v1/memory/search",
                    json={
                        "query": "caching technology",
                        "limit": 5,
                    },
                )
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertIn("results", data)
                self.assertGreater(data["total"], 0)

        self._run(check())

    # ------------------------------------------------------------------
    # 4. Feedback + RL
    # ------------------------------------------------------------------
    def test_04_feedback_rl(self):
        """POST /api/v1/memory/feedback writes reward to Qdrant."""

        async def check():
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=30.0) as c:
                # Store
                sr = await c.post(
                    "/api/v1/memory/store",
                    json={
                        "abstract": "Live test: important architecture decision",
                    },
                )
                uri = sr.json()["uri"]

                # Positive feedback
                r = await c.post(
                    "/api/v1/memory/feedback",
                    json={
                        "uri": uri,
                        "reward": 1.0,
                    },
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["status"], "ok")

                # Negative feedback
                r2 = await c.post(
                    "/api/v1/memory/feedback",
                    json={
                        "uri": uri,
                        "reward": -0.5,
                    },
                )
                self.assertEqual(r2.status_code, 200)

        self._run(check())

    # ------------------------------------------------------------------
    # 5. Decay
    # ------------------------------------------------------------------
    def test_05_decay(self):
        """POST /api/v1/memory/decay triggers Qdrant-level decay."""

        async def check():
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=30.0) as c:
                r = await c.post("/api/v1/memory/decay")
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertIn("records_processed", data)

        self._run(check())

    # ------------------------------------------------------------------
    # 6. Stats
    # ------------------------------------------------------------------
    def test_06_stats(self):
        """GET /api/v1/memory/stats returns Qdrant backend info."""

        async def check():
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=10.0) as c:
                r = await c.get("/api/v1/memory/stats")
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertEqual(data["storage"]["backend"], "qdrant")
                self.assertGreater(data["storage"]["total_records"], 0)

        self._run(check())

    # ------------------------------------------------------------------
    # 7. Full RL pipeline: store → feedback → decay → search
    # ------------------------------------------------------------------
    def test_07_full_rl_pipeline(self):
        """Complete RL pipeline through live HTTP endpoints."""

        async def check():
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=30.0) as c:
                # Store two similar memories
                r1 = await c.post(
                    "/api/v1/memory/store",
                    json={
                        "abstract": "Live pipeline: morning standup discussion about deployment",
                        "category": "live_meetings",
                    },
                )
                uri1 = r1.json()["uri"]

                r2 = await c.post(
                    "/api/v1/memory/store",
                    json={
                        "abstract": "Live pipeline: morning standup discussion about testing",
                        "category": "live_meetings",
                    },
                )
                uri2 = r2.json()["uri"]

                # Boost uri2 with positive feedback
                for _ in range(3):
                    await c.post(
                        "/api/v1/memory/feedback",
                        json={
                            "uri": uri2,
                            "reward": 1.0,
                        },
                    )

                # Decay
                decay = await c.post("/api/v1/memory/decay")
                self.assertEqual(decay.status_code, 200)

                # Search
                sr = await c.post(
                    "/api/v1/memory/search",
                    json={
                        "query": "morning standup",
                        "limit": 5,
                    },
                )
                results = sr.json()["results"]
                found_uris = [r["uri"] for r in results]
                self.assertIn(uri2, found_uris, "Boosted memory should appear")

        self._run(check())

    # ------------------------------------------------------------------
    # 8. Integration endpoints
    # ------------------------------------------------------------------
    def test_08_integration_endpoints(self):
        """Integration verify/doctor/build-agents endpoints work."""

        async def check():
            async with httpx.AsyncClient(base_url=HTTP_BASE, timeout=60.0) as c:
                # Verify
                r1 = await c.get("/api/v1/integration/verify")
                self.assertEqual(r1.status_code, 200)
                self.assertIn("status", r1.json())

                # Doctor
                r2 = await c.get("/api/v1/integration/doctor")
                self.assertEqual(r2.status_code, 200)
                self.assertIn("status", r2.json())

                # Build agents (may involve LLM call, needs longer timeout)
                r3 = await c.get("/api/v1/integration/build-agents")
                self.assertEqual(r3.status_code, 200)
                self.assertIn("agents", r3.json())

        self._run(check())


if __name__ == "__main__":
    unittest.main(verbosity=2)
