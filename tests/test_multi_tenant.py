"""
Multi-tenant integration tests for OpenCortex.

Tests:
- Storing with X-Tenant-ID / X-User-ID headers produces correct URIs
- Storing without headers falls back to config defaults
- Two tenants store and search in isolation (no cross-visibility)
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.core.context import Context
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import (
    get_effective_identity,
    reset_request_identity,
    set_request_identity,
)
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.types import ContextType
from opencortex.storage.vikingdb_interface import VikingDBInterface


# =============================================================================
# Mock Embedder (same as test_e2e_phase1)
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
# In-Memory Storage (same as test_e2e_phase1)
# =============================================================================

class InMemoryStorage(VikingDBInterface):
    def __init__(self):
        self._collections: Dict[str, Dict[str, Any]] = {}
        self._records: Dict[str, Dict[str, Dict[str, Any]]] = {}
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
        return [dict(self._records[collection][rid]) for rid in ids if rid in self._records[collection]]

    async def exists(self, collection, id):
        self._ensure(collection)
        return id in self._records[collection]

    async def batch_insert(self, collection, data):
        return [await self.insert(collection, d) for d in data]

    async def batch_upsert(self, collection, data):
        return [await self.upsert(collection, d) for d in data]

    async def batch_delete(self, collection, filter):
        records = await self.filter(collection, filter, limit=100_000)
        ids = [r["id"] for r in records]
        return await self.delete(collection, ids)

    async def remove_by_uri(self, collection, uri):
        self._ensure(collection)
        to_remove = [rid for rid, rec in self._records[collection].items() if rec.get("uri", "").startswith(uri)]
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

    async def filter(self, collection, filter, limit=10, offset=0, output_fields=None,
                     order_by=None, order_desc=False):
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
        return {
            "collections": len(self._collections),
            "total_records": total,
            "storage_size": 0,
            "backend": "in-memory",
        }

    def _ensure(self, collection):
        if collection not in self._collections:
            from opencortex.storage.vikingdb_interface import CollectionNotFoundError
            raise CollectionNotFoundError(collection)

    @staticmethod
    def _cosine_sim(a, b):
        if not a or not b or len(a) != len(b):
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
        elif op == "and":
            return all(self._eval_filter(record, c) for c in filt.get("conds", []))
        elif op == "or":
            return any(self._eval_filter(record, c) for c in filt.get("conds", []))
        return True


# =============================================================================
# Multi-Tenant Test Suite
# =============================================================================

class TestMultiTenant(unittest.TestCase):
    """Integration tests for multi-tenant identity isolation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="opencortex_mt_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
        )
        init_config(self.config)
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    def _init_orch(self) -> MemoryOrchestrator:
        orch = MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=self.embedder,
        )
        self._run(orch.init())
        return orch

    # -----------------------------------------------------------------
    # 1. Header-based identity → correct URI
    # -----------------------------------------------------------------

    def test_01_store_with_header_identity(self):
        """Storing with request identity produces URI with correct tenant/user."""
        orch = self._init_orch()

        tokens = set_request_identity("teamA", "alice")
        try:
            ctx = self._run(orch.add(
                abstract="Alice's preference on teamA",
                category="preferences",
            ))
        finally:
            reset_request_identity(tokens)

        self.assertIn("teamA", ctx.uri)
        self.assertIn("alice", ctx.uri)
        self.assertNotIn("default-tenant", ctx.uri)
        self.assertNotIn("default-user", ctx.uri)

    # -----------------------------------------------------------------
    # 2. No header → config defaults
    # -----------------------------------------------------------------

    def test_02_store_without_header_uses_defaults(self):
        """Without request identity, URI uses 'default' identity."""
        orch = self._init_orch()

        ctx = self._run(orch.add(
            abstract="Default tenant memory",
            category="general",
        ))

        self.assertIn("default", ctx.uri)

    # -----------------------------------------------------------------
    # 3. Stats reflects effective identity
    # -----------------------------------------------------------------

    def test_03_stats_shows_effective_identity(self):
        """stats() returns per-request identity when contextvar is set."""
        orch = self._init_orch()

        # Without header
        stats = self._run(orch.stats())
        self.assertEqual(stats["tenant_id"], "default")
        self.assertEqual(stats["user_id"], "default")

        # With header
        tokens = set_request_identity("teamB", "bob")
        try:
            stats = self._run(orch.stats())
            self.assertEqual(stats["tenant_id"], "teamB")
            self.assertEqual(stats["user_id"], "bob")
        finally:
            reset_request_identity(tokens)

    # -----------------------------------------------------------------
    # 4. hooks_init reflects effective identity
    # -----------------------------------------------------------------

    def test_04_hooks_init_effective_identity(self):
        """hooks_init returns per-request tenant/user."""
        orch = self._init_orch()

        tokens = set_request_identity("teamC", "charlie")
        try:
            result = self._run(orch.hooks_init())
            self.assertEqual(result["tenant_id"], "teamC")
            self.assertEqual(result["user_id"], "charlie")
        finally:
            reset_request_identity(tokens)

    # -----------------------------------------------------------------
    # 5. Two tenants: store and search in isolation
    # -----------------------------------------------------------------

    def test_05_tenant_isolation(self):
        """Two tenants each store data; each only sees their own."""
        orch = self._init_orch()

        # Tenant A stores
        tokens_a = set_request_identity("tenantA", "userA")
        try:
            ctx_a = self._run(orch.add(
                abstract="TenantA secret data about dark theme",
                category="preferences",
            ))
        finally:
            reset_request_identity(tokens_a)

        # Tenant B stores
        tokens_b = set_request_identity("tenantB", "userB")
        try:
            ctx_b = self._run(orch.add(
                abstract="TenantB secret data about light theme",
                category="preferences",
            ))
        finally:
            reset_request_identity(tokens_b)

        # Verify URIs are different tenants
        self.assertIn("tenantA", ctx_a.uri)
        self.assertIn("tenantB", ctx_b.uri)
        self.assertNotIn("tenantB", ctx_a.uri)
        self.assertNotIn("tenantA", ctx_b.uri)

    # -----------------------------------------------------------------
    # 6. _auto_uri uses effective identity
    # -----------------------------------------------------------------

    def test_06_auto_uri_per_request(self):
        """_auto_uri uses the per-request identity."""
        orch = self._init_orch()

        tokens = set_request_identity("myteam", "myuser")
        try:
            uri = orch._auto_uri("memory", "notes")
        finally:
            reset_request_identity(tokens)

        self.assertIn("myteam", uri)
        self.assertIn("myuser", uri)

    # -----------------------------------------------------------------
    # 7. Concurrent tenants don't interfere
    # -----------------------------------------------------------------

    def test_07_concurrent_tenant_stores(self):
        """Concurrent async adds with different identities produce correct URIs."""
        orch = self._init_orch()

        async def _concurrent():
            results = {}

            async def add_as(name, tenant, user):
                tokens = set_request_identity(tenant, user)
                try:
                    ctx = await orch.add(
                        abstract=f"{name}'s data",
                        category="test",
                    )
                    results[name] = ctx.uri
                finally:
                    reset_request_identity(tokens)

            await asyncio.gather(
                add_as("alice", "team1", "alice"),
                add_as("bob", "team2", "bob"),
            )
            return results

        results = self._run(_concurrent())
        self.assertIn("team1", results["alice"])
        self.assertIn("alice", results["alice"])
        self.assertIn("team2", results["bob"])
        self.assertIn("bob", results["bob"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
