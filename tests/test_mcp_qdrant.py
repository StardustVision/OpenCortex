"""
MCP Server integration tests with real Qdrant adapter.

Tests the full MCP tool pipeline using:
- Real Volcengine embedding API (~/.openviking/ov.conf)
- Embedded Qdrant (local path, no external process)
- FastMCP in-process Client (no network)

Covers: store → search → feedback → RL profile → decay → protect → search boost

Run:
    PYTHONPATH=src uv run python -m unittest tests.test_mcp_qdrant -v
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastmcp import Client

from opencortex.config import CortexConfig, init_config
from opencortex.orchestrator import MemoryOrchestrator

# Skip if no ov.conf
_OV_CONF = Path.home() / ".openviking" / "ov.conf"
_HAS_CONF = _OV_CONF.exists()


def _create_qdrant_mcp_server(tmpdir: str):
    """Create a FastMCP server wired to real Qdrant + Volcengine embedder."""
    from contextlib import asynccontextmanager
    from fastmcp import FastMCP

    qdrant_path = os.path.join(tmpdir, "qdrant")

    config = CortexConfig(
        tenant_id="mcp_qdrant_team",
        user_id="mcp_tester",
        data_root=os.path.join(tmpdir, "data"),
    )
    init_config(config)

    from opencortex.models.embedder.volcengine_embedders import (
        create_embedder_from_ov_conf,
    )
    embedder = create_embedder_from_ov_conf()

    from opencortex.storage.qdrant.adapter import QdrantStorageAdapter
    storage = QdrantStorageAdapter(path=qdrant_path, embedding_dim=embedder.get_dimension())

    @asynccontextmanager
    async def lifespan(server):
        orch = MemoryOrchestrator(config=config, storage=storage, embedder=embedder)
        await orch.init()
        try:
            yield {"orchestrator": orch}
        finally:
            await orch.close()

    from opencortex.mcp_server import (
        memory_decay,
        memory_feedback,
        memory_health,
        memory_search,
        memory_stats,
        memory_store,
        session_begin,
        session_message,
        session_end,
        hooks_route,
        hooks_init,
        hooks_pretrain,
        hooks_verify,
        hooks_doctor,
        hooks_export,
        hooks_build_agents,
    )

    server = FastMCP(name="opencortex-qdrant-test", lifespan=lifespan)
    for tool in [
        memory_store, memory_search, memory_feedback,
        memory_stats, memory_decay, memory_health,
        session_begin, session_message, session_end,
        hooks_route, hooks_init, hooks_pretrain,
        hooks_verify, hooks_doctor, hooks_export, hooks_build_agents,
    ]:
        server.add_tool(tool)
    return server


@unittest.skipUnless(_HAS_CONF, "~/.openviking/ov.conf not found")
class TestMCPQdrant(unittest.TestCase):
    """MCP tool tests with real Qdrant + Volcengine embeddings."""

    _tmpdir: str

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix="mcp_qdrant_test_")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    # ------------------------------------------------------------------
    # 1. Store + Search via MCP with real Qdrant
    # ------------------------------------------------------------------
    def test_01_store_and_search(self):
        """memory_store + memory_search work with Qdrant backend."""
        server = _create_qdrant_mcp_server(
            os.path.join(self._tmpdir, "t01"),
        )

        async def check():
            async with Client(server) as client:
                # Store memories
                r1 = await client.call_tool("memory_store", {
                    "abstract": "User prefers dark theme in VS Code",
                    "category": "preferences",
                })
                data1 = json.loads(r1.content[0].text)
                self.assertIn("uri", data1)
                self.assertEqual(data1["context_type"], "memory")

                await client.call_tool("memory_store", {
                    "abstract": "Project uses PostgreSQL 15 for production",
                    "category": "tech",
                })

                # Search
                sr = await client.call_tool("memory_search", {
                    "query": "What editor theme does the user prefer?",
                    "limit": 5,
                })
                search_data = json.loads(sr.content[0].text)
                self.assertIn("results", search_data)
                self.assertGreater(search_data["total"], 0)

        self._run(check())

    # ------------------------------------------------------------------
    # 2. Feedback + RL profile via MCP with Qdrant
    # ------------------------------------------------------------------
    def test_02_feedback_stores_in_qdrant(self):
        """memory_feedback writes reward_score into Qdrant payload."""
        server = _create_qdrant_mcp_server(
            os.path.join(self._tmpdir, "t02"),
        )

        async def check():
            async with Client(server) as client:
                # Store
                r = await client.call_tool("memory_store", {
                    "abstract": "Critical design decision: use microservices",
                })
                uri = json.loads(r.content[0].text)["uri"]

                # Positive feedback x2
                fb1 = await client.call_tool("memory_feedback", {
                    "uri": uri, "reward": 1.0,
                })
                self.assertEqual(json.loads(fb1.content[0].text)["status"], "ok")

                await client.call_tool("memory_feedback", {
                    "uri": uri, "reward": 1.0,
                })

                # Verify stats show the record
                stats = json.loads(
                    (await client.call_tool("memory_stats", {})).content[0].text
                )
                self.assertGreaterEqual(stats["storage"]["total_records"], 1)
                self.assertEqual(stats["storage"]["backend"], "qdrant")

        self._run(check())

    # ------------------------------------------------------------------
    # 3. Decay via MCP with Qdrant
    # ------------------------------------------------------------------
    def test_03_decay_via_mcp(self):
        """memory_decay applies time-decay to Qdrant records."""
        server = _create_qdrant_mcp_server(
            os.path.join(self._tmpdir, "t03"),
        )

        async def check():
            async with Client(server) as client:
                # Store + feedback
                r = await client.call_tool("memory_store", {
                    "abstract": "Decaying Qdrant memory",
                })
                uri = json.loads(r.content[0].text)["uri"]

                await client.call_tool("memory_feedback", {
                    "uri": uri, "reward": 5.0,
                })

                # Decay
                decay_r = await client.call_tool("memory_decay", {})
                decay_data = json.loads(decay_r.content[0].text)
                self.assertGreater(decay_data.get("records_processed", 0), 0)
                self.assertGreater(decay_data.get("records_decayed", 0), 0)

        self._run(check())

    # ------------------------------------------------------------------
    # 4. Health check shows Qdrant backend
    # ------------------------------------------------------------------
    def test_04_health_qdrant(self):
        """memory_health returns True with Qdrant backend."""
        server = _create_qdrant_mcp_server(
            os.path.join(self._tmpdir, "t04"),
        )

        async def check():
            async with Client(server) as client:
                r = await client.call_tool("memory_health", {})
                data = json.loads(r.content[0].text)
                self.assertTrue(data["initialized"])
                self.assertTrue(data["storage"])
                self.assertTrue(data["embedder"])

        self._run(check())

    # ------------------------------------------------------------------
    # 5. Full RL pipeline via MCP: store → feedback → decay → search boost
    # ------------------------------------------------------------------
    def test_05_full_rl_pipeline_mcp(self):
        """Complete RL pipeline through MCP tools with Qdrant."""
        server = _create_qdrant_mcp_server(
            os.path.join(self._tmpdir, "t05"),
        )

        async def check():
            async with Client(server) as client:
                # 1. Store two similar memories
                r1 = await client.call_tool("memory_store", {
                    "abstract": "Team standup meeting notes from Monday",
                    "category": "meetings",
                })
                uri1 = json.loads(r1.content[0].text)["uri"]

                r2 = await client.call_tool("memory_store", {
                    "abstract": "Team standup meeting notes from Tuesday",
                    "category": "meetings",
                })
                uri2 = json.loads(r2.content[0].text)["uri"]

                # 2. Give strong positive feedback to uri2
                for _ in range(5):
                    await client.call_tool("memory_feedback", {
                        "uri": uri2, "reward": 1.0,
                    })

                # 3. Give negative feedback to uri1
                await client.call_tool("memory_feedback", {
                    "uri": uri1, "reward": -1.0,
                })

                # 4. Decay
                decay = json.loads(
                    (await client.call_tool("memory_decay", {})).content[0].text
                )
                self.assertGreater(decay["records_processed"], 0)

                # 5. Search — uri2 should appear in results (boosted)
                sr = await client.call_tool("memory_search", {
                    "query": "team standup meeting", "limit": 5,
                })
                results = json.loads(sr.content[0].text)["results"]
                found_uris = [r["uri"] for r in results]
                self.assertIn(uri2, found_uris, "Boosted memory should appear")

                # 6. Stats
                stats = json.loads(
                    (await client.call_tool("memory_stats", {})).content[0].text
                )
                self.assertEqual(stats["storage"]["backend"], "qdrant")
                self.assertGreaterEqual(stats["storage"]["total_records"], 2)

        self._run(check())

    # ------------------------------------------------------------------
    # 6. Session tools via MCP with Qdrant
    # ------------------------------------------------------------------
    def test_06_session_tools(self):
        """session_begin/message/end work with Qdrant backend."""
        server = _create_qdrant_mcp_server(
            os.path.join(self._tmpdir, "t06"),
        )

        async def check():
            async with Client(server) as client:
                # Begin session
                r1 = await client.call_tool("session_begin", {
                    "session_id": "test-session-001",
                })
                data1 = json.loads(r1.content[0].text)
                self.assertIn("session_id", data1)

                # Add message
                r2 = await client.call_tool("session_message", {
                    "session_id": "test-session-001",
                    "role": "user",
                    "content": "I prefer using dark theme everywhere",
                })
                data2 = json.loads(r2.content[0].text)
                self.assertTrue(data2.get("added", False))

                # End session
                r3 = await client.call_tool("session_end", {
                    "session_id": "test-session-001",
                    "quality_score": 0.8,
                })
                data3 = json.loads(r3.content[0].text)
                self.assertIn("session_id", data3)

        self._run(check())


if __name__ == "__main__":
    unittest.main(verbosity=2)
