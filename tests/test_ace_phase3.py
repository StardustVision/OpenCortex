"""
ACE Phase 3 tests — Orchestrator integration + HTTP hooks endpoints.

Verifies that ACEngine is auto-created by MemoryOrchestrator.init() and
that all 9 hooks endpoints work end-to-end through both the orchestrator
API and the HTTP server.

Uses in-memory mocks (no external binary or network calls needed).
"""

import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.ace.engine import ACEngine
from opencortex.ace.types import HooksStats
from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.storage.vikingdb_interface import (
    CollectionNotFoundError,
    VikingDBInterface,
)


# =============================================================================
# Mock Embedder (same pattern as Phase 1/2)
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
# In-Memory Storage (same pattern as test_http_server.py)
# =============================================================================


class InMemoryStorage(VikingDBInterface):
    def __init__(self):
        self._collections: Dict[str, Dict[str, Any]] = {}
        self._records: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._rl_profiles: Dict[str, Dict[str, Any]] = {}
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

    # RL stubs
    async def update_reward(self, collection, id, reward):
        key = f"{collection}::{id}"
        p = self._rl_profiles.setdefault(key, {
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
        data = self._rl_profiles.get(key)
        if not data:
            return None
        return _SimpleProfile(id=id, **data)

    async def apply_decay(self):
        processed = decayed = 0
        for p in self._rl_profiles.values():
            processed += 1
            rate = 0.99 if p.get("is_protected") else 0.95
            old = p["effective_score"]
            p["effective_score"] *= rate
            if p["effective_score"] != old:
                decayed += 1
        return _SimpleDecayResult(records_processed=processed, records_decayed=decayed)

    async def set_protected(self, collection, id, protected=True):
        key = f"{collection}::{id}"
        if key in self._rl_profiles:
            self._rl_profiles[key]["is_protected"] = protected

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
# Test Helpers
# =============================================================================


def _run(coro):
    return asyncio.run(coro)


def _make_orchestrator(temp_dir, storage=None, llm_fn=None, hooks=None):
    """Create a MemoryOrchestrator with test config."""
    storage = storage or InMemoryStorage()
    config = CortexConfig(
        tenant_id="testteam",
        user_id="alice",
        data_root=temp_dir,
        embedding_dimension=MockEmbedder.DIMENSION,
    )
    init_config(config)
    embedder = MockEmbedder()
    return MemoryOrchestrator(
        config=config,
        storage=storage,
        embedder=embedder,
        llm_completion=llm_fn,
        hooks=hooks,
    ), storage


# =============================================================================
# TestOrchestratorACE — 8 tests via orchestrator API
# =============================================================================


class TestOrchestratorACE(unittest.TestCase):
    """Test ACEngine integration through MemoryOrchestrator."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_phase3_")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_init_creates_ace_engine(self):
        """init() auto-creates ACEngine when no hooks provided."""
        orch, _ = _make_orchestrator(self.temp_dir)
        _run(orch.init())

        self.assertIsNotNone(orch._hooks)
        self.assertIsInstance(orch._hooks, ACEngine)

    def test_02_hooks_learn_simple(self):
        """hooks_learn returns success=True with new fields."""
        orch, _ = _make_orchestrator(self.temp_dir)
        _run(orch.init())

        result = _run(orch.hooks_learn(state="test state", action="test action", reward=0.5))

        self.assertTrue(result["success"])
        self.assertEqual(result["state"], "test state")
        self.assertIn("best_action", result)
        self.assertIn("message", result)
        self.assertIn("operations_applied", result)
        self.assertIn("reflection_key_insight", result)

    def test_03_hooks_remember_recall(self):
        """remember → recall end-to-end via orchestrator."""
        orch, _ = _make_orchestrator(self.temp_dir)
        _run(orch.init())

        # Remember
        rem_result = _run(orch.hooks_remember(content="Always validate user input", memory_type="strategies"))
        self.assertTrue(rem_result["success"])
        self.assertIn("skill_id", rem_result)

        # Recall
        recall_result = _run(orch.hooks_recall(query="input validation", limit=5))
        self.assertGreater(len(recall_result), 0)
        self.assertIn("content", recall_result[0])

    def test_04_hooks_trajectory_lifecycle(self):
        """begin → step → end via orchestrator."""
        orch, _ = _make_orchestrator(self.temp_dir)
        _run(orch.init())

        # Begin
        begin = _run(orch.hooks_trajectory_begin(trajectory_id="t1", initial_state="start"))
        self.assertEqual(begin["trajectory_id"], "t1")

        # Step
        step = _run(orch.hooks_trajectory_step(trajectory_id="t1", action="act1", reward=0.5))
        self.assertEqual(step["step"], 1)

        # End
        end = _run(orch.hooks_trajectory_end(trajectory_id="t1", quality_score=0.8))
        self.assertEqual(end["trajectory_id"], "t1")
        self.assertEqual(end["steps"], 1)

    def test_05_hooks_error_record_suggest(self):
        """error_record → error_suggest via orchestrator."""
        orch, _ = _make_orchestrator(self.temp_dir)
        _run(orch.init())

        # Record
        rec = _run(orch.hooks_error_record(
            error="JSONDecodeError: invalid UTF-8",
            fix="Validate encoding before JSON.parse",
            context="API response handling",
        ))
        self.assertTrue(rec["success"])
        self.assertIn("skill_id", rec)

        # Suggest
        suggestions = _run(orch.hooks_error_suggest(error="JSONDecodeError"))
        self.assertGreater(len(suggestions), 0)
        self.assertIn("fix", suggestions[0])

    def test_06_hooks_stats(self):
        """hooks_stats returns correct structure."""
        orch, _ = _make_orchestrator(self.temp_dir)
        _run(orch.init())

        # Add some data
        _run(orch.hooks_remember(content="strategy skill", memory_type="strategies"))
        _run(orch.hooks_error_record(error="err", fix="fix it"))

        stats = _run(orch.hooks_stats())
        self.assertTrue(stats["success"])
        self.assertIn("q_learning_patterns", stats)
        self.assertIn("vector_memories", stats)
        self.assertIn("learning_trajectories", stats)
        self.assertIn("error_patterns", stats)
        self.assertEqual(stats["vector_memories"], 2)

    def test_07_hooks_learn_with_llm(self):
        """Full learn pipeline with mock LLM via orchestrator."""
        call_count = []

        async def mock_llm(prompt: str) -> str:
            call_count.append(1)
            if len(call_count) == 1:
                return json.dumps({
                    "reasoning": "Good async usage",
                    "error_identification": "none",
                    "root_cause_analysis": "Correct pattern",
                    "key_insight": "Async is good for IO",
                    "extracted_learnings": [{
                        "learning": "Use async for IO",
                        "evidence": "Worked correctly",
                        "justification": "Non-blocking",
                    }],
                    "skill_tags": [],
                })
            else:
                return json.dumps([{
                    "type": "ADD",
                    "section": "strategies",
                    "content": "Use async for IO operations",
                    "justification": "Non-blocking IO",
                    "evidence": "Worked correctly",
                }])

        orch, _ = _make_orchestrator(self.temp_dir, llm_fn=mock_llm)
        _run(orch.init())

        state = "How to read a file?|||Use async|||aiofiles.open()|||Success"
        result = _run(orch.hooks_learn(state=state, action="aiofiles.open()", reward=1.0))

        self.assertTrue(result["success"])
        self.assertGreater(result["operations_applied"], 0)
        self.assertTrue(result["reflection_key_insight"])

    def test_08_hooks_external_not_overwritten(self):
        """When external hooks are provided, init() does not overwrite them."""

        class FakeHooks:
            """Stub that proves it wasn't replaced."""
            marker = "external"

        fake = FakeHooks()
        orch, _ = _make_orchestrator(self.temp_dir, hooks=fake)
        _run(orch.init())

        self.assertIs(orch._hooks, fake)
        self.assertEqual(orch._hooks.marker, "external")


# =============================================================================
# TestHTTPHooks — 5 tests via HTTP endpoints
# =============================================================================


@asynccontextmanager
async def _test_app_context():
    """Create a FastAPI app wired to in-memory test backends."""
    from fastapi import FastAPI
    import opencortex.http.server as http_server

    temp_dir = tempfile.mkdtemp(prefix="http_ace_test_")
    config = CortexConfig(
        tenant_id="testteam",
        user_id="alice",
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

    import httpx
    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        try:
            yield client
        finally:
            await orch.close()
            http_server._orchestrator = None
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestHTTPHooks(unittest.TestCase):
    """Test hooks endpoints via HTTP."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_09_http_hooks_learn(self):
        """POST /api/v1/hooks/learn returns success with new fields."""
        async def check():
            async with _test_app_context() as client:
                resp = await client.post("/api/v1/hooks/learn", json={
                    "state": "test state",
                    "action": "test action",
                    "reward": 0.5,
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["success"])
                self.assertIn("operations_applied", data)
                self.assertIn("reflection_key_insight", data)

        self._run(check())

    def test_10_http_hooks_remember_recall(self):
        """remember → recall via HTTP endpoints."""
        async def check():
            async with _test_app_context() as client:
                # Remember
                resp = await client.post("/api/v1/hooks/remember", json={
                    "content": "Always use type hints in Python",
                    "memory_type": "strategies",
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["success"])

                # Recall
                resp = await client.post("/api/v1/hooks/recall", json={
                    "query": "type hints",
                    "limit": 5,
                })
                self.assertEqual(resp.status_code, 200)
                results = resp.json()
                self.assertGreater(len(results), 0)
                self.assertIn("content", results[0])

        self._run(check())

    def test_11_http_hooks_trajectory(self):
        """Trajectory lifecycle via HTTP endpoints."""
        async def check():
            async with _test_app_context() as client:
                # Begin
                resp = await client.post("/api/v1/hooks/trajectory/begin", json={
                    "trajectory_id": "t1",
                    "initial_state": "start",
                })
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["trajectory_id"], "t1")

                # Step
                resp = await client.post("/api/v1/hooks/trajectory/step", json={
                    "trajectory_id": "t1",
                    "action": "action1",
                    "reward": 0.5,
                })
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["step"], 1)

                # End
                resp = await client.post("/api/v1/hooks/trajectory/end", json={
                    "trajectory_id": "t1",
                    "quality_score": 0.9,
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertEqual(data["trajectory_id"], "t1")
                self.assertEqual(data["steps"], 1)

        self._run(check())

    def test_12_http_hooks_error(self):
        """error record → suggest via HTTP endpoints."""
        async def check():
            async with _test_app_context() as client:
                # Record
                resp = await client.post("/api/v1/hooks/error/record", json={
                    "error": "TypeError: cannot read property of null",
                    "fix": "Add null check before property access",
                    "context": "Frontend component rendering",
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["success"])

                # Suggest
                resp = await client.post("/api/v1/hooks/error/suggest", json={
                    "error": "TypeError: cannot read property",
                })
                self.assertEqual(resp.status_code, 200)
                results = resp.json()
                self.assertGreater(len(results), 0)
                self.assertIn("fix", results[0])

        self._run(check())

    def test_13_http_hooks_stats(self):
        """GET /api/v1/hooks/stats returns correct structure."""
        async def check():
            async with _test_app_context() as client:
                # Add some data first
                await client.post("/api/v1/hooks/remember", json={
                    "content": "Test memory",
                    "memory_type": "general",
                })

                resp = await client.get("/api/v1/hooks/stats")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["success"])
                self.assertIn("q_learning_patterns", data)
                self.assertIn("vector_memories", data)
                self.assertIn("learning_trajectories", data)
                self.assertIn("error_patterns", data)
                self.assertGreaterEqual(data["vector_memories"], 1)

        self._run(check())


if __name__ == "__main__":
    unittest.main(verbosity=2)
