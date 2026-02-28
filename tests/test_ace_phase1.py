"""
ACE Phase 1 tests — Skillbook CRUD, ACEngine HooksProtocol, CortexFS integration.

Uses in-memory mocks (no external binary or network calls needed).
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

from opencortex.ace.engine import ACEngine
from opencortex.ace.skillbook import Skillbook
from opencortex.ace.types import HooksStats, LearnResult, Skill, UpdateOperation
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.storage.cortex_fs import CortexFS
from opencortex.storage.vikingdb_interface import (
    CollectionNotFoundError,
    VikingDBInterface,
)


# =============================================================================
# Mock Embedder (same pattern as test_e2e_phase1)
# =============================================================================


class MockEmbedder(DenseEmbedderBase):
    DIMENSION = 4

    def __init__(self):
        super().__init__(model_name="mock-embedder-v1")

    def embed(self, text: str) -> EmbedResult:
        vec = self._text_to_vector(text)
        return EmbedResult(dense_vector=vec)

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
# In-Memory Storage (same pattern as test_e2e_phase1)
# =============================================================================


class InMemoryStorage(VikingDBInterface):
    def __init__(self):
        self._collections: Dict[str, Dict[str, Any]] = {}
        self._records: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._closed = False

    async def create_collection(self, name: str, schema: Dict[str, Any]) -> bool:
        if name in self._collections:
            return False
        self._collections[name] = schema
        self._records[name] = {}
        return True

    async def drop_collection(self, name: str) -> bool:
        if name not in self._collections:
            return False
        del self._collections[name]
        del self._records[name]
        return True

    async def collection_exists(self, name: str) -> bool:
        return name in self._collections

    async def list_collections(self) -> List[str]:
        return list(self._collections.keys())

    async def get_collection_info(self, name: str) -> Optional[Dict[str, Any]]:
        if name not in self._collections:
            return None
        return {
            "name": name,
            "vector_dim": self._collections[name].get("vector_dim", 4),
            "count": len(self._records.get(name, {})),
            "status": "ready",
        }

    async def insert(self, collection: str, data: Dict[str, Any]) -> str:
        self._ensure(collection)
        record_id = data.get("id", str(uuid4()))
        data["id"] = record_id
        self._records[collection][record_id] = dict(data)
        return record_id

    async def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        self._ensure(collection)
        if id not in self._records[collection]:
            return False
        self._records[collection][id].update(data)
        return True

    async def upsert(self, collection: str, data: Dict[str, Any]) -> str:
        self._ensure(collection)
        record_id = data.get("id", str(uuid4()))
        data["id"] = record_id
        self._records[collection][record_id] = dict(data)
        return record_id

    async def delete(self, collection: str, ids: List[str]) -> int:
        self._ensure(collection)
        count = 0
        for rid in ids:
            if rid in self._records[collection]:
                del self._records[collection][rid]
                count += 1
        return count

    async def get(self, collection: str, ids: List[str]) -> List[Dict[str, Any]]:
        self._ensure(collection)
        return [
            dict(self._records[collection][rid])
            for rid in ids
            if rid in self._records[collection]
        ]

    async def exists(self, collection: str, id: str) -> bool:
        self._ensure(collection)
        return id in self._records[collection]

    async def batch_insert(self, collection: str, data: List[Dict[str, Any]]) -> List[str]:
        return [await self.insert(collection, d) for d in data]

    async def batch_upsert(self, collection: str, data: List[Dict[str, Any]]) -> List[str]:
        return [await self.upsert(collection, d) for d in data]

    async def batch_delete(self, collection: str, filter: Dict[str, Any]) -> int:
        records = await self.filter(collection, filter, limit=100_000)
        ids = [r["id"] for r in records]
        return await self.delete(collection, ids)

    async def remove_by_uri(self, collection: str, uri: str) -> int:
        self._ensure(collection)
        to_remove = [
            rid
            for rid, rec in self._records[collection].items()
            if rec.get("uri", "").startswith(uri)
        ]
        for rid in to_remove:
            del self._records[collection][rid]
        return len(to_remove)

    async def search(
        self,
        collection: str,
        query_vector: Optional[List[float]] = None,
        sparse_query_vector: Optional[Dict[str, float]] = None,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
        offset: int = 0,
        output_fields: Optional[List[str]] = None,
        with_vector: bool = False,
    ) -> List[Dict[str, Any]]:
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
        return candidates[offset : offset + limit]

    async def filter(
        self,
        collection: str,
        filter: Dict[str, Any],
        limit: int = 10,
        offset: int = 0,
        output_fields: Optional[List[str]] = None,
        order_by: Optional[str] = None,
        order_desc: bool = False,
    ) -> List[Dict[str, Any]]:
        self._ensure(collection)
        candidates = [
            dict(r)
            for r in self._records[collection].values()
            if self._eval_filter(r, filter)
        ]
        if order_by:
            candidates.sort(key=lambda r: r.get(order_by, ""), reverse=order_desc)
        return candidates[offset : offset + limit]

    async def scroll(
        self,
        collection: str,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        offset = int(cursor) if cursor else 0
        records = await self.filter(collection, filter or {}, limit=limit + 1, offset=offset)
        if len(records) > limit:
            return records[:limit], str(offset + limit)
        return records, None

    async def count(self, collection: str, filter: Optional[Dict[str, Any]] = None) -> int:
        self._ensure(collection)
        if filter:
            return len(await self.filter(collection, filter, limit=100_000))
        return len(self._records[collection])

    async def create_index(self, collection: str, field: str, index_type: str, **kw) -> bool:
        return True

    async def drop_index(self, collection: str, field: str) -> bool:
        return True

    async def clear(self, collection: str) -> bool:
        self._ensure(collection)
        self._records[collection].clear()
        return True

    async def optimize(self, collection: str) -> bool:
        return True

    async def close(self) -> None:
        self._closed = True

    async def health_check(self) -> bool:
        return not self._closed

    async def get_stats(self) -> Dict[str, Any]:
        total = sum(len(recs) for recs in self._records.values())
        return {
            "collections": len(self._collections),
            "total_records": total,
            "storage_size": 0,
            "backend": "in-memory",
        }

    def _ensure(self, collection: str) -> None:
        if collection not in self._collections:
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _eval_filter(self, record: Dict[str, Any], filt: Dict[str, Any]) -> bool:
        if not filt:
            return True
        op = filt.get("op", "")
        if op == "must":
            field_name = filt.get("field", "")
            conds = filt.get("conds", [])
            val = record.get(field_name)
            return val in conds
        elif op == "prefix":
            field_name = filt.get("field", "")
            prefix = filt.get("prefix", "")
            val = record.get(field_name, "")
            return str(val).startswith(prefix)
        elif op == "range":
            field_name = filt.get("field", "")
            val = record.get(field_name, 0)
            if "gte" in filt and val < filt["gte"]:
                return False
            if "gt" in filt and val <= filt["gt"]:
                return False
            if "lte" in filt and val > filt["lte"]:
                return False
            if "lt" in filt and val >= filt["lt"]:
                return False
            return True
        elif op == "and":
            return all(self._eval_filter(record, c) for c in filt.get("conds", []))
        elif op == "or":
            return any(self._eval_filter(record, c) for c in filt.get("conds", []))
        return True


# =============================================================================
# Test Helpers
# =============================================================================


def _run(coro):
    return asyncio.run(coro)


# =============================================================================
# Skillbook CRUD Tests
# =============================================================================


class TestSkillbookCRUD(unittest.TestCase):
    """Test Skillbook CRUD operations."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_test_")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()
        self.fs = CortexFS(data_root=self.temp_dir, vector_store=self.storage)
        self.prefix = "opencortex://tenant/test/user/alice/skillbooks"
        self.sb = Skillbook(
            storage=self.storage,
            embedder=self.embedder,
            cortex_fs=self.fs,
            prefix=self.prefix,
            embedding_dim=MockEmbedder.DIMENSION,
        )
        _run(self.sb.init())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_init_creates_collection(self):
        """init() creates the skillbooks collection."""
        self.assertTrue(_run(self.storage.collection_exists("skillbooks")))

    def test_02_add_skill(self):
        """add_skill creates a record in storage and writes CortexFS files."""
        skill = _run(self.sb.add_skill(section="strategies", content="Always use type hints"))
        self.assertIsInstance(skill, Skill)
        self.assertEqual(skill.section, "strategies")
        self.assertEqual(skill.content, "Always use type hints")
        self.assertTrue(skill.created_at)

        # Check storage
        records = _run(self.storage.get("skillbooks", [skill.id]))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["abstract"], "Always use type hints")
        self.assertEqual(records[0]["context_type"], "ace_skill")

        # Check CortexFS abstract file
        abstract_path = os.path.join(
            self.temp_dir,
            "tenant", "test", "user", "alice", "skillbooks",
            "strategies", skill.id, ".abstract.md",
        )
        self.assertTrue(os.path.exists(abstract_path), f"Abstract file should exist at {abstract_path}")

    def test_03_add_skill_auto_id(self):
        """ID auto-generates with correct format: {prefix}-{N:05d}."""
        s1 = _run(self.sb.add_skill(section="strategies", content="skill 1"))
        s2 = _run(self.sb.add_skill(section="strategies", content="skill 2"))
        s3 = _run(self.sb.add_skill(section="error_fixes", content="fix 1"))

        self.assertEqual(s1.id, "strat-00001")
        self.assertEqual(s2.id, "strat-00002")
        self.assertEqual(s3.id, "error-00001")

    def test_04_update_skill_content(self):
        """update_skill changes content and re-embeds."""
        skill = _run(self.sb.add_skill(section="general", content="old content"))
        updated = _run(self.sb.update_skill(skill.id, content="new content"))
        self.assertEqual(updated.content, "new content")

        records = _run(self.storage.get("skillbooks", [skill.id]))
        self.assertEqual(records[0]["abstract"], "new content")

    def test_05_tag_skill(self):
        """tag_skill increments counters."""
        skill = _run(self.sb.add_skill(section="general", content="taggable"))
        _run(self.sb.tag_skill(skill.id, "helpful", 3))
        _run(self.sb.tag_skill(skill.id, "harmful", 1))

        records = _run(self.storage.get("skillbooks", [skill.id]))
        self.assertEqual(records[0]["helpful"], 3)
        self.assertEqual(records[0]["harmful"], 1)
        self.assertEqual(records[0]["active_count"], 4)

    def test_06_remove_skill(self):
        """remove_skill deletes from storage."""
        skill = _run(self.sb.add_skill(section="general", content="to be removed"))
        _run(self.sb.remove_skill(skill.id))

        records = _run(self.storage.get("skillbooks", [skill.id]))
        self.assertEqual(len(records), 0)

    def test_07_get_by_section(self):
        """get_by_section returns only skills in the given section."""
        _run(self.sb.add_skill(section="strategies", content="strategy 1"))
        _run(self.sb.add_skill(section="strategies", content="strategy 2"))
        _run(self.sb.add_skill(section="error_fixes", content="fix 1"))

        strats = _run(self.sb.get_by_section("strategies"))
        self.assertEqual(len(strats), 2)
        for s in strats:
            self.assertEqual(s.section, "strategies")

        fixes = _run(self.sb.get_by_section("error_fixes"))
        self.assertEqual(len(fixes), 1)

    def test_08_search(self):
        """Vector search returns matching skills."""
        _run(self.sb.add_skill(section="strategies", content="Use async await for IO"))
        _run(self.sb.add_skill(section="strategies", content="Prefer composition over inheritance"))
        _run(self.sb.add_skill(section="error_fixes", content="Fix JSON parsing with UTF-8 check"))

        results = _run(self.sb.search("async IO operations"))
        self.assertGreater(len(results), 0)
        self.assertIsInstance(results[0], Skill)

    def test_09_search_with_section_filter(self):
        """Vector search with section filter only returns skills from that section."""
        _run(self.sb.add_skill(section="strategies", content="strategy content"))
        _run(self.sb.add_skill(section="error_fixes", content="error fix content"))

        results = _run(self.sb.search("content", section="error_fixes"))
        for s in results:
            self.assertEqual(s.section, "error_fixes")

    def test_10_as_prompt(self):
        """as_prompt returns tab-separated format with header."""
        _run(self.sb.add_skill(section="strategies", content="Be concise"))
        _run(self.sb.add_skill(section="error_fixes", content="Check encoding"))

        prompt = _run(self.sb.as_prompt())
        lines = prompt.strip().split("\n")
        self.assertEqual(lines[0], "ID\tSection\tContent\tHelpful\tHarmful")
        self.assertEqual(len(lines), 3)  # header + 2 skills

    def test_11_stats(self):
        """stats returns correct counts."""
        _run(self.sb.add_skill(section="strategies", content="s1"))
        _run(self.sb.add_skill(section="strategies", content="s2"))
        _run(self.sb.add_skill(section="error_fixes", content="e1"))

        stats = _run(self.sb.stats())
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["by_section"]["strategies"], 2)
        self.assertEqual(stats["by_section"]["error_fixes"], 1)


# =============================================================================
# ACEngine HooksProtocol Tests
# =============================================================================


class TestACEngineHooks(unittest.TestCase):
    """Test ACEngine's HooksProtocol implementation."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_engine_test_")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()
        self.fs = CortexFS(data_root=self.temp_dir, vector_store=self.storage)
        self.engine = ACEngine(
            storage=self.storage,
            embedder=self.embedder,
            cortex_fs=self.fs,
            tenant_id="test",
            user_id="alice",
        )
        _run(self.engine.init())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_12_remember_recall(self):
        """remember → recall end-to-end."""
        result = _run(self.engine.remember(content="Always validate user input", memory_type="strategies"))
        self.assertTrue(result["success"])
        self.assertIn("skill_id", result)

        recalls = _run(self.engine.recall(query="input validation"))
        self.assertGreater(len(recalls), 0)
        self.assertIn("content", recalls[0])

    def test_13_error_record_suggest(self):
        """error_record → error_suggest end-to-end."""
        record_result = _run(
            self.engine.error_record(
                error="JSONDecodeError: invalid UTF-8",
                fix="Validate encoding before JSON.parse",
                context="API response handling",
            )
        )
        self.assertTrue(record_result["success"])
        self.assertIn("skill_id", record_result)

        suggestions = _run(self.engine.error_suggest(error="JSONDecodeError"))
        self.assertGreater(len(suggestions), 0)
        self.assertIn("fix", suggestions[0])

    def test_14_learn_stub(self):
        """learn() returns LearnResult (simple mode without LLM)."""
        result = _run(self.engine.learn(state="s1", action="a1", reward=0.5))
        self.assertIsInstance(result, LearnResult)
        self.assertTrue(result.success)

    def test_15_trajectory_lifecycle(self):
        """begin → step → end doesn't raise."""
        begin = _run(self.engine.trajectory_begin(trajectory_id="t1", initial_state="start"))
        self.assertEqual(begin["trajectory_id"], "t1")

        step1 = _run(self.engine.trajectory_step(trajectory_id="t1", action="act1", reward=0.5))
        self.assertEqual(step1["step"], 1)

        step2 = _run(
            self.engine.trajectory_step(
                trajectory_id="t1", action="act2", reward=0.8, next_state="s2"
            )
        )
        self.assertEqual(step2["step"], 2)

        end = _run(self.engine.trajectory_end(trajectory_id="t1", quality_score=0.9))
        self.assertEqual(end["steps"], 2)
        self.assertEqual(end["quality_score"], 0.9)

    def test_16_stats_via_engine(self):
        """stats() returns HooksStats with correct counts."""
        _run(self.engine.remember(content="strategy skill", memory_type="strategies"))
        _run(self.engine.error_record(error="err", fix="fix it"))

        stats = _run(self.engine.stats())
        self.assertIsInstance(stats, HooksStats)
        self.assertEqual(stats.vector_memories, 2)
        self.assertGreaterEqual(stats.error_patterns, 1)


# =============================================================================
# CortexFS Three-Layer Tests
# =============================================================================


class TestCortexFSIntegration(unittest.TestCase):
    """Test CortexFS three-layer write from Skillbook."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_fs_test_")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()
        self.fs = CortexFS(data_root=self.temp_dir, vector_store=self.storage)
        self.prefix = "opencortex://tenant/test/user/alice/skillbooks"
        self.sb = Skillbook(
            storage=self.storage,
            embedder=self.embedder,
            cortex_fs=self.fs,
            prefix=self.prefix,
            embedding_dim=MockEmbedder.DIMENSION,
        )
        _run(self.sb.init())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_17_persist_writes_l0_l1_l2(self):
        """CortexFS writes L0 (abstract), L1 (overview), L2 (content/trace) files."""
        skill = _run(
            self.sb.add_skill(
                section="patterns",
                content="Use retry with exponential backoff",
                justification="Prevents thundering herd",
                evidence="Production incident 2024-01",
                trace="Full trace log of the retry pattern implementation",
            )
        )

        base = os.path.join(
            self.temp_dir,
            "tenant", "test", "user", "alice", "skillbooks",
            "patterns", skill.id,
        )

        # L0: abstract
        abstract_path = os.path.join(base, ".abstract.md")
        self.assertTrue(os.path.exists(abstract_path))
        with open(abstract_path) as f:
            self.assertEqual(f.read(), "Use retry with exponential backoff")

        # L1: overview
        overview_path = os.path.join(base, ".overview.md")
        self.assertTrue(os.path.exists(overview_path))
        with open(overview_path) as f:
            overview = f.read()
            self.assertIn("Justification", overview)
            self.assertIn("Prevents thundering herd", overview)
            self.assertIn("Evidence", overview)

        # L2: content (trace)
        content_path = os.path.join(base, "content.md")
        self.assertTrue(os.path.exists(content_path))
        with open(content_path) as f:
            self.assertIn("Full trace log", f.read())

    def test_18_section_summary_update(self):
        """update_section_summary writes section-level abstract and overview."""
        _run(self.sb.add_skill(section="strategies", content="Skill A"))
        _run(self.sb.add_skill(section="strategies", content="Skill B"))
        _run(self.sb.update_section_summary("strategies"))

        section_base = os.path.join(
            self.temp_dir,
            "tenant", "test", "user", "alice", "skillbooks",
            "strategies",
        )

        abstract_path = os.path.join(section_base, ".abstract.md")
        self.assertTrue(os.path.exists(abstract_path))
        with open(abstract_path) as f:
            abstract = f.read()
            self.assertIn("strategies", abstract)
            self.assertIn("2 skills", abstract)

        overview_path = os.path.join(section_base, ".overview.md")
        self.assertTrue(os.path.exists(overview_path))
        with open(overview_path) as f:
            overview = f.read()
            self.assertIn("Skill A", overview)
            self.assertIn("Skill B", overview)


if __name__ == "__main__":
    unittest.main(verbosity=2)
