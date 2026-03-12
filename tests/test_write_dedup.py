"""
Write-time semantic deduplication tests.

Validates that orchestrator.add(dedup=True) correctly detects and handles
duplicates:  same abstract → skip/merge, different abstract → create,
dedup=False → force write, cross-tenant → no dedup.
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.http.request_context import (
    set_request_identity,
    reset_request_identity,
)
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.storage.storage_interface import (
    CollectionNotFoundError,
    StorageInterface,
)


# =========================================================================
# Mock Embedder (same as test_e2e_phase1)
# =========================================================================


class MockEmbedder(DenseEmbedderBase):
    """Word-bag embedder for dedup testing.

    Uses a 32-dimensional vector where each dimension corresponds to
    a hash bucket of the input words.  Same text → identical vector
    (cosine=1.0).  Different text → low similarity.
    """

    DIMENSION = 32

    def __init__(self):
        super().__init__(model_name="mock-embedder-dedup")

    def embed(self, text: str) -> EmbedResult:
        return EmbedResult(dense_vector=self._text_to_vector(text))

    def get_dimension(self) -> int:
        return self.DIMENSION

    @staticmethod
    def _text_to_vector(text: str) -> List[float]:
        vec = [0.0] * 32
        for word in text.lower().split():
            idx = hash(word) % 32
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


# =========================================================================
# In-Memory Storage (minimal, from test_e2e_phase1)
# =========================================================================


class InMemoryStorage(StorageInterface):
    def __init__(self):
        self._collections: Dict[str, Dict[str, Any]] = {}
        self._records: Dict[str, Dict[str, Dict[str, Any]]] = {}

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
        return {"name": name, "vector_dim": 32, "count": len(self._records.get(name, {})), "status": "ready"}

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
                     filter=None, limit=10, offset=0, output_fields=None,
                     with_vector=False, text_query=""):
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
        pass

    async def health_check(self):
        return True

    async def get_stats(self):
        total = sum(len(recs) for recs in self._records.values())
        return {"collections": len(self._collections), "total_records": total, "storage_size": 0, "backend": "in-memory"}

    # RL methods
    async def update_reward(self, collection, id, reward):
        self._ensure(collection)
        record = self._records[collection].get(id)
        if record:
            record["reward_score"] = record.get("reward_score", 0.0) + reward

    async def get_profile(self, collection, id):
        return None

    async def apply_decay(self, decay_rate=0.95, protected_rate=0.99, threshold=0.01):
        class _R:
            records_processed = 0
            records_decayed = 0
            records_below_threshold = 0
            records_archived = 0
        return _R()

    async def set_protected(self, collection, id, protected=True):
        pass

    # Internal
    def _ensure(self, collection):
        if collection not in self._collections:
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")

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
            field_name = filt.get("field", "")
            conds = filt.get("conds", [])
            val = record.get(field_name)
            if val is None and "" in conds:
                return True
            return val in conds
        elif op == "prefix":
            return str(record.get(filt.get("field", ""), "")).startswith(filt.get("prefix", ""))
        elif op == "range":
            field_name = filt.get("field", "")
            val = record.get(field_name, 0)
            if "gte" in filt and val < filt["gte"]:
                return False
            if "lte" in filt and val > filt["lte"]:
                return False
            return True
        elif op == "and":
            return all(self._eval_filter(record, c) for c in filt.get("conds", []))
        elif op == "or":
            return any(self._eval_filter(record, c) for c in filt.get("conds", []))
        elif op == "must_not":
            field_name = filt.get("field", "")
            conds = filt.get("conds", [])
            val = record.get(field_name)
            return val not in conds
        return True


# =========================================================================
# Tests
# =========================================================================


class TestWriteDedup(unittest.TestCase):
    """Write-time semantic deduplication tests."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="opencortex_dedup_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
        )
        init_config(self.config)
        self._identity_tokens = set_request_identity("testteam", "alice")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()

    def tearDown(self):
        reset_request_identity(self._identity_tokens)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_orch(self):
        orch = MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=self.embedder,
        )
        self._run(orch.init())
        return orch

    def _record_count(self):
        """Count leaf records in the context collection."""
        return len([
            r for r in self.storage._records.get("context", {}).values()
            if r.get("is_leaf", False)
        ])

    # -----------------------------------------------------------------
    # 1. Same abstract twice → second should be skipped (non-mergeable)
    # -----------------------------------------------------------------

    def test_same_abstract_non_mergeable_skipped(self):
        """Identical abstract in 'events' category → second add() skipped."""
        orch = self._make_orch()

        # No content to avoid enrichment changing the abstract vector
        ctx1 = self._run(orch.add(
            abstract="Server crashed at 3am due to OOM",
            category="events",
            dedup=True,
        ))
        self.assertEqual(ctx1.meta.get("dedup_action"), "created")

        ctx2 = self._run(orch.add(
            abstract="Server crashed at 3am due to OOM",
            category="events",
            dedup=True,
        ))
        self.assertEqual(ctx2.meta.get("dedup_action"), "skipped")
        self.assertEqual(ctx2.uri, ctx1.uri)
        # Only 1 leaf record
        self.assertEqual(self._record_count(), 1)

    # -----------------------------------------------------------------
    # 2. Same abstract + mergeable category → merged
    # -----------------------------------------------------------------

    def test_same_abstract_mergeable_merged(self):
        """Identical abstract in 'preferences' → second add() merged."""
        orch = self._make_orch()

        ctx1 = self._run(orch.add(
            abstract="User prefers dark theme in all editors",
            category="preferences",
            dedup=True,
        ))
        self.assertEqual(ctx1.meta.get("dedup_action"), "created")

        # Same abstract, no content → identical vector → dedup triggers
        ctx2 = self._run(orch.add(
            abstract="User prefers dark theme in all editors",
            category="preferences",
            dedup=True,
        ))
        self.assertEqual(ctx2.meta.get("dedup_action"), "merged")
        self.assertEqual(ctx2.uri, ctx1.uri)
        self.assertEqual(self._record_count(), 1)

    # -----------------------------------------------------------------
    # 3. Different abstract → created normally
    # -----------------------------------------------------------------

    def test_different_abstract_created(self):
        """Different abstracts → both created."""
        orch = self._make_orch()

        ctx1 = self._run(orch.add(
            abstract="User prefers dark theme",
            category="preferences",
            dedup=True,
        ))
        ctx2 = self._run(orch.add(
            abstract="Database connection pool uses 20 connections",
            category="preferences",
            dedup=True,
        ))
        self.assertEqual(ctx1.meta.get("dedup_action"), "created")
        self.assertEqual(ctx2.meta.get("dedup_action"), "created")
        self.assertNotEqual(ctx1.uri, ctx2.uri)
        self.assertEqual(self._record_count(), 2)

    # -----------------------------------------------------------------
    # 4. dedup=False → force write even if duplicate
    # -----------------------------------------------------------------

    def test_dedup_false_forces_write(self):
        """dedup=False bypasses dedup check."""
        orch = self._make_orch()

        ctx1 = self._run(orch.add(
            abstract="Same content here",
            category="events",
        ))
        ctx2 = self._run(orch.add(
            abstract="Same content here",
            category="events",
            dedup=False,
        ))
        self.assertEqual(ctx1.meta.get("dedup_action"), "created")
        self.assertEqual(ctx2.meta.get("dedup_action"), "created")
        self.assertNotEqual(ctx1.uri, ctx2.uri)
        self.assertEqual(self._record_count(), 2)

    # -----------------------------------------------------------------
    # 5. Cross-tenant → no dedup (different tenant_id)
    # -----------------------------------------------------------------

    def test_cross_tenant_no_dedup(self):
        """Records from different tenants should not dedup each other."""
        orch = self._make_orch()

        # Add as testteam/alice
        ctx1 = self._run(orch.add(
            abstract="Shared knowledge item",
            category="patterns",
            dedup=True,
        ))
        self.assertEqual(ctx1.meta.get("dedup_action"), "created")

        # Switch to different tenant
        reset_request_identity(self._identity_tokens)
        self._identity_tokens = set_request_identity("otherteam", "bob")

        ctx2 = self._run(orch.add(
            abstract="Shared knowledge item",
            category="patterns",
            dedup=True,
        ))
        self.assertEqual(ctx2.meta.get("dedup_action"), "created")
        self.assertNotEqual(ctx1.uri, ctx2.uri)

    # -----------------------------------------------------------------
    # 6. Cross-category → no dedup
    # -----------------------------------------------------------------

    def test_cross_category_no_dedup(self):
        """Same abstract but different category → both created."""
        orch = self._make_orch()

        ctx1 = self._run(orch.add(
            abstract="Important finding about performance",
            category="events",
            dedup=True,
        ))
        ctx2 = self._run(orch.add(
            abstract="Important finding about performance",
            category="patterns",
            dedup=True,
        ))
        self.assertEqual(ctx1.meta.get("dedup_action"), "created")
        self.assertEqual(ctx2.meta.get("dedup_action"), "created")

    # -----------------------------------------------------------------
    # 7. Non-leaf nodes bypass dedup
    # -----------------------------------------------------------------

    def test_non_leaf_bypasses_dedup(self):
        """Directory nodes (is_leaf=False) should not be deduped."""
        orch = self._make_orch()

        ctx1 = self._run(orch.add(
            abstract="Directory abstract",
            is_leaf=False,
            dedup=True,
        ))
        ctx2 = self._run(orch.add(
            abstract="Directory abstract",
            is_leaf=False,
            dedup=True,
        ))
        # Both created because dedup skips non-leaf
        self.assertEqual(ctx1.meta.get("dedup_action"), "created")
        self.assertEqual(ctx2.meta.get("dedup_action"), "created")

    # -----------------------------------------------------------------
    # 8. Merged content is appended
    # -----------------------------------------------------------------

    def test_merged_content_appended(self):
        """When merging, new content is appended to existing."""
        orch = self._make_orch()

        # First add without content → vector = pure abstract
        self._run(orch.add(
            abstract="User prefers vim keybindings",
            category="preferences",
            dedup=True,
        ))
        # Second add same abstract → triggers dedup merge
        ctx2 = self._run(orch.add(
            abstract="User prefers vim keybindings",
            category="preferences",
            dedup=True,
        ))
        self.assertEqual(ctx2.meta.get("dedup_action"), "merged")

        # Verify only 1 leaf record
        records = list(self.storage._records.get("context", {}).values())
        leaf_records = [r for r in records if r.get("is_leaf", False) and r.get("category") == "preferences"]
        self.assertEqual(len(leaf_records), 1)

    # -----------------------------------------------------------------
    # 9. Dedup with no embedder → skips dedup gracefully
    # -----------------------------------------------------------------

    def test_no_embedder_skips_dedup(self):
        """Without an embedder, dedup is skipped (no vector)."""
        orch = MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=None,
        )
        self._run(orch.init())

        ctx1 = self._run(orch.add(abstract="No embedder test", category="events"))
        ctx2 = self._run(orch.add(abstract="No embedder test", category="events"))
        # Both created because no vector → dedup condition not met
        self.assertEqual(ctx1.meta.get("dedup_action"), "created")
        self.assertEqual(ctx2.meta.get("dedup_action"), "created")


if __name__ == "__main__":
    unittest.main()
