"""
HTTP Server end-to-end tests for OpenCortex.

Uses httpx.AsyncClient + ASGITransport to test FastAPI endpoints
with InMemoryStorage + MockEmbedder (no external dependencies).
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

import httpx
from httpx import ASGITransport

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.storage.storage_interface import (
    CollectionNotFoundError,
    StorageInterface,
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
# In-Memory Storage (same as test_e2e_phase1.py / test_mcp_server.py)
# =============================================================================


class InMemoryStorage(StorageInterface):
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

    async def search(
        self,
        collection,
        query_vector=None,
        sparse_query_vector=None,
        filter=None,
        limit=10,
        offset=0,
        output_fields=None,
        with_vector=False,
        text_query=None,
    ):
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

    # Reinforcement Learning
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
# Test App Factory
# =============================================================================


@asynccontextmanager
async def _test_app_context():
    """Create a FastAPI app wired to in-memory test backends.

    Yields an httpx.AsyncClient bound to the ASGI app.
    Manually manages the orchestrator lifecycle since httpx ASGITransport
    does not trigger ASGI lifespan events.
    """
    from fastapi import FastAPI
    import opencortex.http.server as http_server

    temp_dir = tempfile.mkdtemp(prefix="http_test_")
    config = CortexConfig(
        data_root=temp_dir,
        embedding_dimension=MockEmbedder.DIMENSION,
        rerank_provider="disabled",
        query_classifier_enabled=False,
        hyde_enabled=False,
        explain_enabled=False,
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
# HTTP Server Test Suite
# =============================================================================


class TestHTTPServer(unittest.TestCase):
    """End-to-end HTTP endpoint tests."""

    def _run(self, coro):
        return asyncio.run(coro)

    # -----------------------------------------------------------------
    # 1. Health endpoint
    # -----------------------------------------------------------------

    def test_01_health(self):
        """GET /api/v1/memory/health returns component status."""
        async def check():
            async with _test_app_context() as client:
                resp = await client.get("/api/v1/memory/health")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["initialized"])
                self.assertTrue(data["storage"])
                self.assertTrue(data["embedder"])

        self._run(check())

    # -----------------------------------------------------------------
    # 2. Store
    # -----------------------------------------------------------------

    def test_02_store(self):
        """POST /api/v1/memory/store creates a context."""
        async def check():
            async with _test_app_context() as client:
                resp = await client.post("/api/v1/memory/store", json={
                    "abstract": "User prefers dark theme",
                    "category": "preferences",
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("uri", data)
                self.assertIn("default", data["uri"])
                self.assertEqual(data["context_type"], "memory")

        self._run(check())

    # -----------------------------------------------------------------
    # 3. Search
    # -----------------------------------------------------------------

    def test_03_search(self):
        """POST /api/v1/memory/search returns results after storing."""
        async def check():
            async with _test_app_context() as client:
                await client.post("/api/v1/memory/store", json={
                    "abstract": "User prefers dark theme in editors",
                    "category": "preferences",
                })
                await client.post("/api/v1/memory/store", json={
                    "abstract": "Project uses Python 3.12",
                    "category": "tech",
                })
                resp = await client.post("/api/v1/memory/search", json={
                    "query": "What theme does the user prefer?",
                    "limit": 5,
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("results", data)
                self.assertGreater(len(data["results"]), 0)
                self.assertGreater(data["total"], 0)
                self.assertIn("memory_pipeline", data)
                self.assertIn("probe", data["memory_pipeline"])
                self.assertIn("planner", data["memory_pipeline"])
                self.assertIn("runtime", data["memory_pipeline"])
                self.assertNotIn("search_intent", data)
                self.assertNotIn("recall_plan", data)
                self.assertTrue(data["memory_pipeline"]["probe"]["should_recall"])
                self.assertIn(
                    data["memory_pipeline"]["probe"]["scope_source"],
                    {"target_uri", "session_id", "source_doc_id", "context_type", "global_root"},
                )
                self.assertIn(
                    "scope_authoritative",
                    data["memory_pipeline"]["probe"],
                )
                self.assertIn(
                    "selected_root_uris",
                    data["memory_pipeline"]["probe"],
                )
                planned_depth = data["memory_pipeline"]["planner"]["retrieval_depth"]
                self.assertIn(planned_depth, {"l0", "l1"})
                effective_depth = data["memory_pipeline"]["runtime"]["trace"][
                    "effective"
                ]["retrieval_depth"]
                self.assertIn(effective_depth, {"l0", "l1", "l2"})
                self.assertIn(
                    effective_depth,
                    {planned_depth, "l2"},
                )
                self.assertIn(
                    "probe",
                    data["memory_pipeline"]["runtime"]["trace"],
                )
                self.assertTrue(
                    {
                        "anchor_hits",
                        "candidate_entries",
                        "evidence",
                        "should_recall",
                        "trace",
                        "scope_source",
                        "scope_authoritative",
                        "selected_root_uris",
                    }.issubset(
                        set(
                            data["memory_pipeline"]["runtime"]["trace"][
                                "probe"
                            ].keys()
                        )
                    )
                )
                self.assertIn(
                    "latency_ms",
                    data["memory_pipeline"]["runtime"]["trace"],
                )
                self.assertEqual(
                    sorted(
                        data["memory_pipeline"]["runtime"]["trace"][
                            "latency_ms"
                        ]["stages"
                        ].keys()
                    ),
                    ["aggregate", "bind", "hydrate", "plan", "probe", "retrieve", "total"],
                )
                self.assertEqual(
                    sorted(
                        data["memory_pipeline"]["runtime"]["trace"][
                            "latency_ms"
                        ]["retrieve"
                        ].keys()
                    ),
                    ["assemble", "embed", "rerank", "search", "total"],
                )
                self.assertIn(
                    "hydration",
                    data["memory_pipeline"]["runtime"]["trace"],
                )

        self._run(check())

    def test_03b_intent_should_recall_returns_phase1_contract(self):
        """POST /api/v1/intent/should_recall returns phase-1 probe semantics."""
        async def check():
            async with _test_app_context() as client:
                resp = await client.post("/api/v1/intent/should_recall", json={
                    "query": "What did we discuss yesterday?",
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(
                    {
                        "should_recall",
                        "anchor_hits",
                        "candidate_entries",
                        "evidence",
                        "trace",
                        "scope_source",
                        "scope_authoritative",
                        "selected_root_uris",
                    }.issubset(set(data.keys()))
                )
                self.assertTrue(data["should_recall"])
                self.assertIn("candidate_count", data["evidence"])
                self.assertIn("top_k", data["trace"])

        self._run(check())

    def test_03c_memory_search_exposes_probe_and_runtime_contract_flags(self):
        """POST /api/v1/memory/search exposes scoped contract flags."""
        async def check():
            async with _test_app_context() as client:
                await client.post("/api/v1/memory/store", json={
                    "abstract": "Launch plan notes for the active project",
                    "category": "events",
                })
                resp = await client.post("/api/v1/memory/search", json={
                    "query": "launch notes",
                    "limit": 5,
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("memory_pipeline", data)
                self.assertIn(
                    "scoped_miss",
                    data["memory_pipeline"]["probe"],
                )
                self.assertIn(
                    "fallback_ready",
                    data["memory_pipeline"]["probe"],
                )
                self.assertFalse(data["memory_pipeline"]["probe"]["fallback_ready"])
                self.assertIn("runtime", data["memory_pipeline"])
                self.assertIn(
                    "fallback",
                    data["memory_pipeline"]["runtime"]["trace"],
                )
                self.assertEqual(
                    data["memory_pipeline"]["runtime"]["trace"]["fallback"],
                    [],
                )

        self._run(check())

    # -----------------------------------------------------------------
    # 4. Feedback
    # -----------------------------------------------------------------

    def test_04_feedback(self):
        """POST /api/v1/memory/feedback sends reward."""
        async def check():
            async with _test_app_context() as client:
                store_resp = await client.post("/api/v1/memory/store", json={
                    "abstract": "Important design decision",
                })
                uri = store_resp.json()["uri"]

                resp = await client.post("/api/v1/memory/feedback", json={
                    "uri": uri,
                    "reward": 1.0,
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertEqual(data["status"], "ok")
                self.assertEqual(data["uri"], uri)

        self._run(check())

    # -----------------------------------------------------------------
    # 5. Stats
    # -----------------------------------------------------------------

    def test_05_stats(self):
        """GET /api/v1/memory/stats returns statistics."""
        async def check():
            async with _test_app_context() as client:
                resp = await client.get("/api/v1/memory/stats")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertEqual(data["tenant_id"], "default")
                self.assertEqual(data["user_id"], "default")
                self.assertIn("storage", data)

        self._run(check())

    # -----------------------------------------------------------------
    # 6. Decay
    # -----------------------------------------------------------------

    def test_06_decay(self):
        """POST /api/v1/memory/decay triggers time-decay."""
        async def check():
            async with _test_app_context() as client:
                # Store + feedback to create RL profile
                store_resp = await client.post("/api/v1/memory/store", json={
                    "abstract": "Decaying memory",
                })
                uri = store_resp.json()["uri"]
                await client.post("/api/v1/memory/feedback", json={
                    "uri": uri,
                    "reward": 5.0,
                })

                resp = await client.post("/api/v1/memory/decay")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertGreaterEqual(data.get("records_processed", 0), 0)

        self._run(check())

    # -----------------------------------------------------------------
    # 7. Full pipeline via HTTP
    # -----------------------------------------------------------------

    def test_07_full_pipeline(self):
        """Complete pipeline: store -> search -> feedback -> decay via HTTP."""
        async def check():
            async with _test_app_context() as client:
                # 1. Store memories
                uris = []
                for text in [
                    "User prefers dark theme in VS Code",
                    "Team uses PostgreSQL for production",
                    "Deploy via GitHub Actions CI/CD",
                ]:
                    r = await client.post("/api/v1/memory/store", json={
                        "abstract": text, "category": "general",
                    })
                    self.assertEqual(r.status_code, 200)
                    uris.append(r.json()["uri"])

                # 2. Verify stats
                stats = (await client.get("/api/v1/memory/stats")).json()
                self.assertGreaterEqual(stats["storage"]["total_records"], 3)

                # 3. Search
                search = (await client.post("/api/v1/memory/search", json={
                    "query": "database", "limit": 3,
                })).json()
                self.assertGreater(search["total"], 0)

                # 4. Feedback
                fb = (await client.post("/api/v1/memory/feedback", json={
                    "uri": uris[0], "reward": 1.0,
                })).json()
                self.assertEqual(fb["status"], "ok")

                # 5. Decay
                decay = (await client.post("/api/v1/memory/decay")).json()
                self.assertGreaterEqual(decay.get("records_processed", 0), 0)

                # 6. Health check
                health = (await client.get("/api/v1/memory/health")).json()
                self.assertTrue(health["initialized"])

        self._run(check())

    # -----------------------------------------------------------------
    # 8. System status endpoint
    # -----------------------------------------------------------------

    def test_08_system_status_doctor(self):
        """GET /api/v1/system/status returns doctor report by default."""
        async def check():
            async with _test_app_context() as client:
                resp = await client.get("/api/v1/system/status")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("issues", data)
                self.assertIn("initialized", data)

        self._run(check())

    def test_09_system_status_health(self):
        """GET /api/v1/system/status?type=health returns health check."""
        async def check():
            async with _test_app_context() as client:
                resp = await client.get("/api/v1/system/status?type=health")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("initialized", data)
                self.assertIn("storage", data)

        self._run(check())

    def test_10_system_status_stats(self):
        """GET /api/v1/system/status?type=stats returns statistics."""
        async def check():
            async with _test_app_context() as client:
                resp = await client.get("/api/v1/system/status?type=stats")
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("storage", data)
                self.assertIn("tenant_id", data)

        self._run(check())


if __name__ == "__main__":
    unittest.main(verbosity=2)
