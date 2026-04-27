"""
Unit tests for the Qdrant storage adapter.

Tests:
- Filter translator (Filter DSL → Qdrant Filter)
- QdrantStorageAdapter CRUD operations
- Vector search (dense)
- scroll / count operations

Uses Qdrant's embedded local mode (temp directory) — no external service needed.
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.storage.qdrant.adapter import QdrantStorageAdapter
from opencortex.storage.qdrant.filter_translator import translate_filter
from opencortex.storage.storage_interface import CollectionNotFoundError


# =============================================================================
# Test data helpers
# =============================================================================

VECTOR_DIM = 4

_CONTEXT_SCHEMA = {
    "CollectionName": "context",
    "Description": "Test collection",
    "Fields": [
        {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
        {"FieldName": "uri", "FieldType": "path"},
        {"FieldName": "context_type", "FieldType": "string"},
        {"FieldName": "vector", "FieldType": "vector", "Dim": VECTOR_DIM},
        {"FieldName": "sparse_vector", "FieldType": "sparse_vector"},
        {"FieldName": "abstract", "FieldType": "string"},
        {"FieldName": "parent_uri", "FieldType": "path"},
        {"FieldName": "is_leaf", "FieldType": "bool"},
        {"FieldName": "active_count", "FieldType": "int64"},
    ],
    "ScalarIndex": [
        "uri",
        "context_type",
        "parent_uri",
        "is_leaf",
        "active_count",
    ],
}


def _make_vector(seed: int) -> List[float]:
    """Deterministic unit vector for testing."""
    raw = [
        ((seed >> 0) & 0xFF) / 255.0,
        ((seed >> 8) & 0xFF) / 255.0,
        ((seed >> 16) & 0xFF) / 255.0,
        ((seed >> 24) & 0xFF) / 255.0,
    ]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def _make_record(
    id: str,
    uri: str,
    abstract: str,
    context_type: str = "memory",
    is_leaf: bool = True,
    parent_uri: str = "",
    active_count: int = 0,
    seed: int = 42,
) -> Dict[str, Any]:
    return {
        "id": id,
        "uri": uri,
        "abstract": abstract,
        "context_type": context_type,
        "is_leaf": is_leaf,
        "parent_uri": parent_uri,
        "active_count": active_count,
        "vector": _make_vector(seed),
    }


# =============================================================================
# Filter translator tests
# =============================================================================


class TestFilterTranslator(unittest.TestCase):
    """Unit tests for the Filter DSL → Qdrant Filter translator."""

    def test_empty_filter(self):
        """Empty dict returns empty filter."""
        f = translate_filter({})
        self.assertIsNotNone(f)

    def test_must_single(self):
        """must with single value → MatchValue."""
        f = translate_filter({"op": "must", "field": "context_type", "conds": ["memory"]})
        self.assertTrue(f.must)
        self.assertEqual(len(f.must), 1)

    def test_must_multi(self):
        """must with multiple values → MatchAny."""
        f = translate_filter({"op": "must", "field": "context_type", "conds": ["memory", "skill"]})
        self.assertTrue(f.must)

    def test_range(self):
        """range filter."""
        f = translate_filter({"op": "range", "field": "active_count", "gte": 1, "lt": 100})
        self.assertTrue(f.must)

    def test_prefix(self):
        """prefix filter."""
        f = translate_filter({"op": "prefix", "field": "uri", "prefix": "opencortex://"})
        self.assertTrue(f.must)

    def test_contains(self):
        """contains filter."""
        f = translate_filter({"op": "contains", "field": "abstract", "substring": "theme"})
        self.assertTrue(f.must)

    def test_and(self):
        """and with nested conditions."""
        f = translate_filter({
            "op": "and",
            "conds": [
                {"op": "must", "field": "context_type", "conds": ["memory"]},
                {"op": "must", "field": "is_leaf", "conds": [True]},
            ],
        })
        self.assertTrue(f.must)
        self.assertEqual(len(f.must), 2)

    def test_or(self):
        """or with nested conditions."""
        f = translate_filter({
            "op": "or",
            "conds": [
                {"op": "must", "field": "context_type", "conds": ["memory"]},
                {"op": "must", "field": "context_type", "conds": ["skill"]},
            ],
        })
        self.assertTrue(f.should)
        self.assertEqual(len(f.should), 2)

    def test_must_not(self):
        """must_not filter."""
        f = translate_filter({"op": "must_not", "field": "context_type", "conds": ["skill"]})
        self.assertTrue(f.must_not)

    def test_unknown_operator_raises(self):
        """Unknown filter operators must not become match-all filters."""
        with self.assertRaises(ValueError):
            translate_filter({"op": "equals", "field": "context_type", "value": "memory"})


# =============================================================================
# QdrantStorageAdapter tests
# =============================================================================


class TestQdrantAdapter(unittest.TestCase):
    """Integration tests for QdrantStorageAdapter using embedded local mode."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="qdrant_test_")
        self.adapter = QdrantStorageAdapter(
            path=self.temp_dir,
            embedding_dim=VECTOR_DIM,
        )

    def tearDown(self):
        asyncio.run(self._cleanup())

    async def _cleanup(self):
        try:
            await self.adapter.close()
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    # ---- Collection Management ----

    def test_create_collection(self):
        """Create collection and verify it exists."""
        result = self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))
        self.assertTrue(result)

        exists = self._run(self.adapter.collection_exists("context"))
        self.assertTrue(exists)

    def test_create_duplicate_collection(self):
        """Creating duplicate collection returns False."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))
        result = self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))
        self.assertFalse(result)

    def test_drop_collection(self):
        """Drop collection removes it."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))
        result = self._run(self.adapter.drop_collection("context"))
        self.assertTrue(result)
        self.assertFalse(self._run(self.adapter.collection_exists("context")))

    def test_list_collections(self):
        """List collections returns created collection names."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))
        cols = self._run(self.adapter.list_collections())
        self.assertIn("context", cols)

    def test_get_collection_info(self):
        """Get collection info returns metadata."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))
        info = self._run(self.adapter.get_collection_info("context"))
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "context")

    # ---- Single CRUD ----

    def test_insert_and_get(self):
        """Insert a record and retrieve it by ID."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        rec = _make_record("rec1", "opencortex://t/u/m/1", "Dark theme preference")
        rid = self._run(self.adapter.insert("context", rec))
        self.assertIsNotNone(rid)

        results = self._run(self.adapter.get("context", ["rec1"]))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["abstract"], "Dark theme preference")

    def test_update(self):
        """Update modifies payload fields."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        rec = _make_record("rec1", "opencortex://t/u/m/1", "Original")
        self._run(self.adapter.insert("context", rec))

        success = self._run(self.adapter.update("context", "rec1", {"abstract": "Updated"}))
        self.assertTrue(success)

        results = self._run(self.adapter.get("context", ["rec1"]))
        self.assertEqual(results[0]["abstract"], "Updated")

    def test_update_not_found(self):
        """Update returns False for missing record."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))
        success = self._run(self.adapter.update("context", "nonexistent", {"abstract": "x"}))
        self.assertFalse(success)

    def test_upsert(self):
        """Upsert creates or updates a record."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        rec = _make_record("rec1", "opencortex://t/u/m/1", "Version 1")
        self._run(self.adapter.upsert("context", rec))

        # Upsert same ID with different abstract
        rec2 = _make_record("rec1", "opencortex://t/u/m/1", "Version 2")
        self._run(self.adapter.upsert("context", rec2))

        results = self._run(self.adapter.get("context", ["rec1"]))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["abstract"], "Version 2")

    def test_delete(self):
        """Delete removes records by ID."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        self._run(self.adapter.insert("context",
            _make_record("r1", "opencortex://t/u/m/1", "A")))
        self._run(self.adapter.insert("context",
            _make_record("r2", "opencortex://t/u/m/2", "B")))

        count = self._run(self.adapter.delete("context", ["r1"]))
        self.assertEqual(count, 1)

        self.assertFalse(self._run(self.adapter.exists("context", "r1")))
        self.assertTrue(self._run(self.adapter.exists("context", "r2")))

    def test_exists(self):
        """exists returns True for existing record, False otherwise."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        self._run(self.adapter.insert("context",
            _make_record("r1", "opencortex://t/u/m/1", "A")))

        self.assertTrue(self._run(self.adapter.exists("context", "r1")))
        self.assertFalse(self._run(self.adapter.exists("context", "nonexistent")))

    # ---- Batch CRUD ----

    def test_batch_insert(self):
        """Batch insert creates multiple records."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        records = [
            _make_record(f"r{i}", f"opencortex://t/u/m/{i}", f"Memory {i}", seed=i * 100)
            for i in range(5)
        ]
        ids = self._run(self.adapter.batch_insert("context", records))
        self.assertEqual(len(ids), 5)

        count = self._run(self.adapter.count("context"))
        self.assertEqual(count, 5)

    # ---- Search ----

    def test_dense_search(self):
        """Dense vector search returns scored results."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        for i in range(5):
            self._run(self.adapter.insert("context",
                _make_record(f"r{i}", f"opencortex://t/u/m/{i}",
                             f"Memory {i}", seed=i * 1000 + 42)))

        query_vector = _make_vector(42)  # Should match r0 exactly
        results = self._run(self.adapter.search(
            "context",
            query_vector=query_vector,
            limit=3,
        ))

        self.assertGreater(len(results), 0)
        # First result should have highest score
        self.assertIn("_score", results[0])
        self.assertGreater(results[0]["_score"], 0)

    def test_search_with_filter(self):
        """Search with scalar filter narrows results."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        self._run(self.adapter.insert("context",
            _make_record("r1", "opencortex://t/u/m/1", "Memory A",
                         context_type="memory", seed=100)))
        self._run(self.adapter.insert("context",
            _make_record("r2", "opencortex://t/u/r/1", "Resource B",
                         context_type="resource", seed=200)))

        query_vector = _make_vector(100)
        results = self._run(self.adapter.search(
            "context",
            query_vector=query_vector,
            filter={"op": "must", "field": "context_type", "conds": ["memory"]},
            limit=10,
        ))

        for r in results:
            self.assertEqual(r.get("context_type"), "memory")

    def test_filter_only_search(self):
        """Search without vector uses scalar filtering (scroll)."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        self._run(self.adapter.insert("context",
            _make_record("r1", "opencortex://t/u/m/1", "A", context_type="memory")))
        self._run(self.adapter.insert("context",
            _make_record("r2", "opencortex://t/u/r/1", "B", context_type="resource")))

        results = self._run(self.adapter.search(
            "context",
            filter={"op": "must", "field": "context_type", "conds": ["memory"]},
            limit=10,
        ))

        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r.get("context_type"), "memory")

    # ---- Filter (scalar only) ----

    def test_filter_method(self):
        """filter() method returns records matching conditions."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        self._run(self.adapter.insert("context",
            _make_record("r1", "opencortex://t/u/m/1", "A", context_type="memory")))
        self._run(self.adapter.insert("context",
            _make_record("r2", "opencortex://t/u/r/1", "B", context_type="resource")))
        self._run(self.adapter.insert("context",
            _make_record("r3", "opencortex://t/u/m/2", "C", context_type="memory")))

        results = self._run(self.adapter.filter(
            "context",
            {"op": "must", "field": "context_type", "conds": ["memory"]},
            limit=10,
        ))

        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r.get("context_type"), "memory")

    def test_filter_method_with_order_by_missing_field(self):
        """filter(order_by=...) should not drop records lacking that field."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        self._run(self.adapter.insert("context",
            _make_record("r1", "opencortex://t/u/m/1", "A", context_type="memory")))
        self._run(self.adapter.insert("context",
            _make_record("r2", "opencortex://t/u/m/2", "B", context_type="memory")))

        results = self._run(self.adapter.filter(
            "context",
            {"op": "must", "field": "context_type", "conds": ["memory"]},
            limit=10,
            order_by="updated_at",
            order_desc=True,
        ))

        self.assertEqual(len(results), 2)

    def test_filter_order_by_uses_bounded_native_scroll(self):
        """filter(order_by=...) should not drain every matching page."""

        class FakeClient:
            def __init__(self):
                self.scroll_calls = []

            async def collection_exists(self, name):
                return True

            async def scroll(self, **kwargs):
                self.scroll_calls.append(kwargs)
                return [
                    SimpleNamespace(
                        id="r-new",
                        payload={
                            "id": "r-new",
                            "uri": "opencortex://t/u/m/new",
                            "abstract": "new",
                            "updated_at": "2026-01-02T00:00:00Z",
                        },
                        vector=None,
                    )
                ], None

        fake_client = FakeClient()
        self.adapter._client = fake_client

        results = self._run(self.adapter.filter(
            "context",
            {"op": "must", "field": "context_type", "conds": ["memory"]},
            limit=1,
            order_by="updated_at",
            order_desc=True,
        ))

        self.assertEqual([r["id"] for r in results], ["r-new"])
        self.assertEqual(len(fake_client.scroll_calls), 1)
        self.assertEqual(fake_client.scroll_calls[0]["limit"], 1)
        self.assertIsNotNone(fake_client.scroll_calls[0]["order_by"])

    # ---- Scroll ----

    def test_scroll(self):
        """scroll() paginates through all records."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        for i in range(10):
            self._run(self.adapter.insert("context",
                _make_record(f"r{i}", f"opencortex://t/u/m/{i}", f"M{i}", seed=i * 100)))

        all_records = []
        cursor = None
        while True:
            records, cursor = self._run(self.adapter.scroll(
                "context", limit=3, cursor=cursor))
            all_records.extend(records)
            if cursor is None:
                break

        self.assertEqual(len(all_records), 10)

    # ---- Count ----

    def test_count(self):
        """count() returns correct number of records."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        for i in range(5):
            self._run(self.adapter.insert("context",
                _make_record(f"r{i}", f"opencortex://t/u/m/{i}", f"M{i}", seed=i * 100)))

        total = self._run(self.adapter.count("context"))
        self.assertEqual(total, 5)

    def test_count_with_filter(self):
        """count() with filter returns filtered count."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        self._run(self.adapter.insert("context",
            _make_record("r1", "opencortex://t/u/m/1", "A", context_type="memory")))
        self._run(self.adapter.insert("context",
            _make_record("r2", "opencortex://t/u/r/1", "B", context_type="resource")))
        self._run(self.adapter.insert("context",
            _make_record("r3", "opencortex://t/u/m/2", "C", context_type="memory")))

        count = self._run(self.adapter.count(
            "context",
            {"op": "must", "field": "context_type", "conds": ["memory"]},
        ))
        self.assertEqual(count, 2)

    # ---- Lifecycle ----

    def test_clear(self):
        """clear() removes all records but keeps collection."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        for i in range(3):
            self._run(self.adapter.insert("context",
                _make_record(f"r{i}", f"opencortex://t/u/m/{i}", f"M{i}", seed=i * 100)))

        self.assertEqual(self._run(self.adapter.count("context")), 3)

        self._run(self.adapter.clear("context"))

        self.assertEqual(self._run(self.adapter.count("context")), 0)
        self.assertTrue(self._run(self.adapter.collection_exists("context")))

    def test_health_check(self):
        """health_check() returns True for embedded mode."""
        self.assertTrue(self._run(self.adapter.health_check()))

    def test_get_stats(self):
        """get_stats() returns backend info."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))
        stats = self._run(self.adapter.get_stats())
        self.assertEqual(stats["backend"], "qdrant")
        self.assertGreaterEqual(stats["collections"], 1)

    # ---- Error handling ----

    def test_operation_on_missing_collection(self):
        """Operations on non-existent collection raise CollectionNotFoundError."""
        with self.assertRaises(CollectionNotFoundError):
            self._run(self.adapter.insert("nonexistent", {"id": "1"}))

    def test_remove_by_uri(self):
        """remove_by_uri deletes matching records."""
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

        self._run(self.adapter.insert("context",
            _make_record("r1", "opencortex://t/u/m/1", "A")))
        self._run(self.adapter.insert("context",
            _make_record("r2", "opencortex://t/u/m/2", "B")))
        self._run(self.adapter.insert("context",
            _make_record("r3", "opencortex://t/u/r/1", "C")))

        removed = self._run(self.adapter.remove_by_uri("context", "opencortex://t/u/m"))
        self.assertGreaterEqual(removed, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
