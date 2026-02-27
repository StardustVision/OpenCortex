"""
MCP Server end-to-end tests for OpenCortex.

Uses FastMCP in-process Client to call tools directly against the MCP server
with an InMemoryStorage backend (no external dependencies).
"""

import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastmcp import Client

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.storage.vikingdb_interface import (
    CollectionNotFoundError,
    VikingDBInterface,
)


# =============================================================================
# Mock Embedder (same as test_e2e_phase1.py)
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
# In-Memory Storage (same as test_e2e_phase1.py)
# =============================================================================


class InMemoryStorage(VikingDBInterface):
    def __init__(self):
        self._collections: Dict[str, Dict[str, Any]] = {}
        self._records: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._sona_profiles: Dict[str, Dict[str, Any]] = {}
        self._closed = False

    async def create_collection(self, name, schema):
        if name in self._collections:
            return False
        self._collections[name] = schema
        self._records[name] = {}
        return True

    async def drop_collection(self, name):
        if name not in self._collections:
            return False
        del self._collections[name]
        del self._records[name]
        return True

    async def collection_exists(self, name):
        return name in self._collections

    async def list_collections(self):
        return list(self._collections.keys())

    async def get_collection_info(self, name):
        if name not in self._collections:
            return None
        return {
            "name": name,
            "vector_dim": self._collections[name].get("vector_dim", 4),
            "count": len(self._records.get(name, {})),
            "status": "ready",
        }

    async def insert(self, collection, data):
        self._ensure(collection)
        rid = data.get("id", str(uuid4()))
        data["id"] = rid
        self._records[collection][rid] = dict(data)
        return rid

    async def update(self, collection, id, data):
        self._ensure(collection)
        if id not in self._records[collection]:
            return False
        self._records[collection][id].update(data)
        return True

    async def upsert(self, collection, data):
        self._ensure(collection)
        rid = data.get("id", str(uuid4()))
        data["id"] = rid
        self._records[collection][rid] = dict(data)
        return rid

    async def delete(self, collection, ids):
        self._ensure(collection)
        count = 0
        for rid in ids:
            if rid in self._records[collection]:
                del self._records[collection][rid]
                count += 1
        return count

    async def get(self, collection, ids):
        self._ensure(collection)
        return [dict(self._records[collection][r]) for r in ids if r in self._records[collection]]

    async def exists(self, collection, id):
        self._ensure(collection)
        return id in self._records[collection]

    async def batch_insert(self, collection, data):
        return [await self.insert(collection, d) for d in data]

    async def batch_upsert(self, collection, data):
        return [await self.upsert(collection, d) for d in data]

    async def batch_delete(self, collection, filter):
        records = await self.filter(collection, filter, limit=100_000)
        return await self.delete(collection, [r["id"] for r in records])

    async def remove_by_uri(self, collection, uri):
        self._ensure(collection)
        to_remove = [rid for rid, r in self._records[collection].items() if r.get("uri", "").startswith(uri)]
        for rid in to_remove:
            del self._records[collection][rid]
        return len(to_remove)

    async def search(self, collection, query_vector=None, sparse_query_vector=None,
                     filter=None, limit=10, offset=0, output_fields=None, with_vector=False):
        self._ensure(collection)
        candidates = list(self._records[collection].values())
        if filter:
            candidates = [r for r in candidates if self._eval_filter(r, filter)]
        if query_vector:
            scored = []
            for r in candidates:
                vec = r.get("vector")
                score = self._cosine_sim(query_vector, vec) if vec else 0.0
                rec = dict(r)
                rec["_score"] = score
                scored.append(rec)
            scored.sort(key=lambda x: x["_score"], reverse=True)
            candidates = scored
        return candidates[offset:offset + limit]

    async def filter(self, collection, filter, limit=10, offset=0,
                     output_fields=None, order_by=None, order_desc=False):
        self._ensure(collection)
        candidates = [dict(r) for r in self._records[collection].values() if self._eval_filter(r, filter)]
        if order_by:
            candidates.sort(key=lambda r: r.get(order_by, ""), reverse=order_desc)
        return candidates[offset:offset + limit]

    async def scroll(self, collection, filter=None, limit=100, cursor=None, output_fields=None):
        offset = int(cursor) if cursor else 0
        records = await self.filter(collection, filter or {}, limit=limit + 1, offset=offset)
        if len(records) > limit:
            return records[:limit], str(offset + limit)
        return records, None

    async def count(self, collection, filter=None):
        self._ensure(collection)
        if filter:
            return len(await self.filter(collection, filter, limit=100_000))
        return len(self._records[collection])

    async def create_index(self, collection, field, index_type, **kw):
        return True

    async def drop_index(self, collection, field):
        return True

    async def clear(self, collection):
        self._ensure(collection)
        self._records[collection].clear()
        return True

    async def optimize(self, collection):
        return True

    async def close(self):
        self._closed = True

    async def health_check(self):
        return not self._closed

    async def get_stats(self):
        total = sum(len(recs) for recs in self._records.values())
        return {"collections": len(self._collections), "total_records": total,
                "storage_size": 0, "backend": "in-memory"}

    # SONA
    async def update_reward(self, collection, id, reward):
        key = f"{collection}::{id}"
        p = self._sona_profiles.setdefault(key, {
            "reward_score": 0.0, "retrieval_count": 0,
            "positive_feedback_count": 0, "negative_feedback_count": 0,
            "effective_score": 0.0, "is_protected": False,
        })
        p["reward_score"] += reward
        p["retrieval_count"] += 1
        if reward > 0:
            p["positive_feedback_count"] += 1
        elif reward < 0:
            p["negative_feedback_count"] += 1
        p["effective_score"] = p["reward_score"]

    async def get_profile(self, collection, id):
        key = f"{collection}::{id}"
        data = self._sona_profiles.get(key)
        if not data:
            return None
        return _SimpleProfile(id=id, **data)

    async def apply_decay(self):
        processed = decayed = 0
        for p in self._sona_profiles.values():
            processed += 1
            rate = 0.99 if p.get("is_protected") else 0.95
            old = p["effective_score"]
            p["effective_score"] *= rate
            if p["effective_score"] != old:
                decayed += 1
        return _SimpleDecayResult(records_processed=processed, records_decayed=decayed)

    async def set_protected(self, collection, id, protected=True):
        key = f"{collection}::{id}"
        if key in self._sona_profiles:
            self._sona_profiles[key]["is_protected"] = protected

    def _ensure(self, collection):
        if collection not in self._collections:
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")

    @staticmethod
    def _cosine_sim(a, b):
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def _eval_filter(self, record, filt):
        if not filt:
            return True
        op = filt.get("op", "")
        if op == "must":
            return record.get(filt.get("field", "")) in filt.get("conds", [])
        elif op == "prefix":
            return str(record.get(filt.get("field", ""), "")).startswith(filt.get("prefix", ""))
        elif op == "range":
            val = record.get(filt.get("field", ""), 0)
            if "gte" in filt and val < filt["gte"]:
                return False
            if "gt" in filt and val <= filt["gt"]:
                return False
            if "lte" in filt and val > filt["lte"]:
                return False
            if "lt" in filt and val >= filt["lt"]:
                return False
            return True
        elif op == "contains":
            return filt.get("substring", "") in str(record.get(filt.get("field", ""), ""))
        elif op == "and":
            return all(self._eval_filter(record, c) for c in filt.get("conds", []))
        elif op == "or":
            return any(self._eval_filter(record, c) for c in filt.get("conds", []))
        return True


@dataclass
class _SimpleProfile:
    id: str = ""
    reward_score: float = 0.0
    retrieval_count: int = 0
    positive_feedback_count: int = 0
    negative_feedback_count: int = 0
    last_retrieved_at: float = 0.0
    last_feedback_at: float = 0.0
    effective_score: float = 0.0
    is_protected: bool = False


@dataclass
class _SimpleDecayResult:
    records_processed: int = 0
    records_decayed: int = 0
    records_below_threshold: int = 0
    records_archived: int = 0


# =============================================================================
# MCP Server Test Suite
# =============================================================================


def _create_test_server():
    """Create a FastMCP server wired to in-memory test backends."""
    from contextlib import asynccontextmanager
    from fastmcp import FastMCP

    temp_dir = tempfile.mkdtemp(prefix="mcp_test_")
    config = CortexConfig(
        tenant_id="testteam",
        user_id="alice",
        data_root=temp_dir,
        embedding_dimension=MockEmbedder.DIMENSION,
    )
    init_config(config)

    storage = InMemoryStorage()
    embedder = MockEmbedder()

    @asynccontextmanager
    async def lifespan(server):
        orch = MemoryOrchestrator(config=config, storage=storage, embedder=embedder)
        await orch.init()
        try:
            yield {"orchestrator": orch}
        finally:
            await orch.close()
            shutil.rmtree(temp_dir, ignore_errors=True)

    # Import the tools from the real mcp_server module and re-register on a
    # test server with our custom lifespan.
    from opencortex.mcp_server import (
        memory_decay,
        memory_feedback,
        memory_health,
        memory_search,
        memory_stats,
        memory_store,
        # Session tools
        session_begin,
        session_message,
        session_end,
        # Hooks integration tools
        hooks_route,
        hooks_init,
        hooks_pretrain,
        hooks_verify,
        hooks_doctor,
        hooks_export,
        hooks_build_agents,
    )

    server = FastMCP(name="opencortex-test", lifespan=lifespan)
    # Core memory tools
    server.add_tool(memory_store)
    server.add_tool(memory_search)
    server.add_tool(memory_feedback)
    server.add_tool(memory_stats)
    server.add_tool(memory_decay)
    server.add_tool(memory_health)
    # Session tools
    server.add_tool(session_begin)
    server.add_tool(session_message)
    server.add_tool(session_end)
    # Hooks integration tools
    server.add_tool(hooks_route)
    server.add_tool(hooks_init)
    server.add_tool(hooks_pretrain)
    server.add_tool(hooks_verify)
    server.add_tool(hooks_doctor)
    server.add_tool(hooks_export)
    server.add_tool(hooks_build_agents)
    return server


class TestMCPServer(unittest.TestCase):
    """End-to-end MCP tool invocation tests."""

    def _run(self, coro):
        return asyncio.run(coro)

    # -----------------------------------------------------------------
    # 1. List tools
    # -----------------------------------------------------------------

    def test_01_list_tools(self):
        """Server exposes all expected tools."""
        server = _create_test_server()

        async def check():
            async with Client(server) as client:
                tools = await client.list_tools()
                names = {t.name for t in tools}
                # Core memory tools
                for expected in [
                    "memory_store", "memory_search", "memory_feedback",
                    "memory_stats", "memory_decay", "memory_health",
                ]:
                    self.assertIn(expected, names)
                # Session tools
                for expected in ["session_begin", "session_message", "session_end"]:
                    self.assertIn(expected, names)
                # Hooks integration tools
                for expected in [
                    "hooks_route", "hooks_init", "hooks_pretrain",
                    "hooks_verify", "hooks_doctor", "hooks_export",
                    "hooks_build_agents",
                ]:
                    self.assertIn(expected, names)

        self._run(check())

    # -----------------------------------------------------------------
    # 2. memory_store
    # -----------------------------------------------------------------

    def test_02_memory_store(self):
        """memory_store creates a context and returns URI."""
        server = _create_test_server()

        async def check():
            async with Client(server) as client:
                result = await client.call_tool("memory_store", {
                    "abstract": "User prefers dark theme",
                    "category": "preferences",
                })
                # call_tool returns CallToolResult with content
                data = json.loads(result.content[0].text)
                self.assertIn("uri", data)
                self.assertIn("testteam", data["uri"])
                self.assertEqual(data["context_type"], "memory")
                self.assertEqual(data["category"], "preferences")

        self._run(check())

    # -----------------------------------------------------------------
    # 3. memory_search
    # -----------------------------------------------------------------

    def test_03_memory_search(self):
        """memory_search returns relevant results after storing memories."""
        server = _create_test_server()

        async def check():
            async with Client(server) as client:
                # Store some memories
                await client.call_tool("memory_store", {
                    "abstract": "User prefers dark theme in editors",
                    "category": "preferences",
                })
                await client.call_tool("memory_store", {
                    "abstract": "Project uses Python 3.12",
                    "category": "tech",
                })

                # Search
                result = await client.call_tool("memory_search", {
                    "query": "What theme does the user prefer?",
                    "limit": 5,
                })
                data = json.loads(result.content[0].text)
                self.assertIn("results", data)
                self.assertIn("total", data)
                self.assertGreater(data["total"], 0)

        self._run(check())

    # -----------------------------------------------------------------
    # 4. memory_feedback
    # -----------------------------------------------------------------

    def test_04_memory_feedback(self):
        """memory_feedback sends reward signal without error."""
        server = _create_test_server()

        async def check():
            async with Client(server) as client:
                # Store a memory first
                store_result = await client.call_tool("memory_store", {
                    "abstract": "Important design decision",
                })
                uri = json.loads(store_result.content[0].text)["uri"]

                # Send feedback
                fb_result = await client.call_tool("memory_feedback", {
                    "uri": uri,
                    "reward": 1.0,
                })
                data = json.loads(fb_result.content[0].text)
                self.assertEqual(data["status"], "ok")
                self.assertEqual(data["uri"], uri)

        self._run(check())

    # -----------------------------------------------------------------
    # 5. memory_stats
    # -----------------------------------------------------------------

    def test_05_memory_stats(self):
        """memory_stats returns system statistics."""
        server = _create_test_server()

        async def check():
            async with Client(server) as client:
                result = await client.call_tool("memory_stats", {})
                data = json.loads(result.content[0].text)
                self.assertEqual(data["tenant_id"], "testteam")
                self.assertEqual(data["user_id"], "alice")
                self.assertIn("storage", data)
                self.assertEqual(data["storage"]["backend"], "in-memory")

        self._run(check())

    # -----------------------------------------------------------------
    # 6. memory_decay
    # -----------------------------------------------------------------

    def test_06_memory_decay(self):
        """memory_decay triggers time-decay successfully."""
        server = _create_test_server()

        async def check():
            async with Client(server) as client:
                # Store + feedback to create SONA profile
                store_result = await client.call_tool("memory_store", {
                    "abstract": "Decaying memory",
                })
                uri = json.loads(store_result.content[0].text)["uri"]
                await client.call_tool("memory_feedback", {"uri": uri, "reward": 5.0})

                # Trigger decay
                result = await client.call_tool("memory_decay", {})
                data = json.loads(result.content[0].text)
                self.assertGreaterEqual(data.get("records_processed", 0), 0)

        self._run(check())

    # -----------------------------------------------------------------
    # 7. memory_health
    # -----------------------------------------------------------------

    def test_07_memory_health(self):
        """memory_health returns component health status."""
        server = _create_test_server()

        async def check():
            async with Client(server) as client:
                result = await client.call_tool("memory_health", {})
                data = json.loads(result.content[0].text)
                self.assertTrue(data["initialized"])
                self.assertTrue(data["storage"])
                self.assertTrue(data["embedder"])

        self._run(check())

    # -----------------------------------------------------------------
    # 8. Full pipeline via MCP
    # -----------------------------------------------------------------

    def test_08_full_pipeline(self):
        """Complete pipeline: store -> search -> feedback -> decay via MCP tools."""
        server = _create_test_server()

        async def check():
            async with Client(server) as client:
                # 1. Store memories
                uris = []
                for text in [
                    "User prefers dark theme in VS Code",
                    "Team uses PostgreSQL for production",
                    "Deploy via GitHub Actions CI/CD",
                ]:
                    r = await client.call_tool("memory_store", {
                        "abstract": text, "category": "general",
                    })
                    uris.append(json.loads(r.content[0].text)["uri"])

                # 2. Verify stats
                stats = json.loads(
                    (await client.call_tool("memory_stats", {})).content[0].text
                )
                self.assertGreaterEqual(stats["storage"]["total_records"], 3)

                # 3. Search
                search = json.loads(
                    (await client.call_tool("memory_search", {
                        "query": "database", "limit": 3,
                    })).content[0].text
                )
                self.assertGreater(search["total"], 0)

                # 4. Feedback on first stored memory
                fb = json.loads(
                    (await client.call_tool("memory_feedback", {
                        "uri": uris[0], "reward": 1.0,
                    })).content[0].text
                )
                self.assertEqual(fb["status"], "ok")

                # 5. Decay
                decay = json.loads(
                    (await client.call_tool("memory_decay", {})).content[0].text
                )
                self.assertGreaterEqual(decay.get("records_processed", 0), 0)

                # 6. Health check
                health = json.loads(
                    (await client.call_tool("memory_health", {})).content[0].text
                )
                self.assertTrue(health["initialized"])

        self._run(check())


if __name__ == "__main__":
    unittest.main(verbosity=2)
