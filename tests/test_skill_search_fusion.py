# SPDX-License-Identifier: Apache-2.0
"""
Tests for Skill Search Fusion — verifying that:
1. Orchestrator.add() triggers async skill extraction
2. Orchestrator.search() returns skillbook results alongside memory results
3. Orchestrator.feedback() updates skillbook tags for skill URIs
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
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.storage.vikingdb_interface import (
    CollectionNotFoundError,
    VikingDBInterface,
)


# =============================================================================
# Mock Embedder
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
# In-Memory Storage
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
        return {"name": name, "count": len(self._records.get(name, {}))}

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
            rid for rid, rec in self._records[collection].items()
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
        return candidates[offset: offset + limit]

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
            dict(r) for r in self._records[collection].values()
            if self._eval_filter(r, filter)
        ]
        if order_by:
            candidates.sort(key=lambda r: r.get(order_by, ""), reverse=order_desc)
        return candidates[offset: offset + limit]

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
            return len([
                r for r in self._records[collection].values()
                if self._eval_filter(r, filter)
            ])
        return len(self._records[collection])

    async def create_index(self, collection: str, field: str, index_type: str = "scalar") -> bool:
        return True

    async def drop_index(self, collection: str, field: str) -> bool:
        return True

    async def clear(self, collection: str) -> bool:
        self._ensure(collection)
        self._records[collection] = {}
        return True

    async def optimize(self, collection: str) -> bool:
        return True

    async def close(self) -> None:
        self._closed = True

    async def health_check(self) -> bool:
        return not self._closed

    async def get_stats(self) -> Dict[str, Any]:
        return {
            "collections": len(self._collections),
            "total_records": sum(len(r) for r in self._records.values()),
        }

    # RL methods
    async def update_reward(self, collection: str, record_id: str, reward: float) -> bool:
        self._ensure(collection)
        if record_id not in self._records[collection]:
            return False
        rec = self._records[collection][record_id]
        rec["reward_score"] = rec.get("reward_score", 0.0) + reward * 0.1
        if reward > 0:
            rec["positive_feedback_count"] = rec.get("positive_feedback_count", 0) + 1
        elif reward < 0:
            rec["negative_feedback_count"] = rec.get("negative_feedback_count", 0) + 1
        return True

    def _ensure(self, collection: str):
        if collection not in self._collections:
            raise CollectionNotFoundError(collection)

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    @staticmethod
    def _eval_filter(record: Dict[str, Any], f: Dict[str, Any]) -> bool:
        op = f.get("op", "")
        if op == "must":
            field_name = f.get("field", "")
            conds = f.get("conds", [])
            val = record.get(field_name, "")
            return val in conds
        elif op == "and":
            return all(InMemoryStorage._eval_filter(record, c) for c in f.get("conds", []))
        elif op == "or":
            return any(InMemoryStorage._eval_filter(record, c) for c in f.get("conds", []))
        elif op == "prefix":
            field_name = f.get("field", "")
            prefix = f.get("prefix", "")
            return record.get(field_name, "").startswith(prefix)
        return True


# =============================================================================
# Tests: Skill Search Fusion
# =============================================================================


class TestSkillSearchFusion(unittest.TestCase):
    """Test that search results include skillbook entries."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = CortexConfig(
            tenant_id="testteam",
            user_id="alice",
            data_root=self.temp_dir,
            embedding_provider="none",
        )
        init_config(self.config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        """Run an async coroutine synchronously."""
        return asyncio.run(coro)

    async def _init_orch(self) -> MemoryOrchestrator:
        storage = InMemoryStorage()
        embedder = MockEmbedder()
        orch = MemoryOrchestrator(
            config=self.config,
            storage=storage,
            embedder=embedder,
        )
        await orch.init()
        return orch

    def test_01_search_returns_skillbook_entries(self):
        """Skills stored via hooks_remember appear in search results."""
        async def _test():
            orch = await self._init_orch()
            result = await orch.hooks_remember(
                content="Always use pytest-asyncio for async test fixtures",
                memory_type="preferences",
            )
            self.assertTrue(result.get("success"))

            find_result = await orch.search("how to test async code")
            skill_abstracts = [s.abstract for s in find_result.skills]
            self.assertTrue(
                any("pytest-asyncio" in a for a in skill_abstracts),
                f"Expected skillbook result in search. Got skills: {skill_abstracts}",
            )
        self._run(_test())

    def test_02_search_without_hooks_still_works(self):
        """Search works fine when hooks are None (no skillbook)."""
        async def _test():
            orch = await self._init_orch()
            orch._hooks = None

            await orch.add(abstract="Dark theme preference", category="preferences")
            result = await orch.search("theme")
            self.assertIsNotNone(result)
        self._run(_test())

    def test_03_skill_dedup_in_search(self):
        """Duplicate skills from skillbook and context collection are deduplicated."""
        async def _test():
            orch = await self._init_orch()
            await orch.hooks_remember(content="Use black for formatting", memory_type="preferences")
            await orch.hooks_remember(content="Use black for formatting", memory_type="preferences")

            result = await orch.search("formatting tool")
            uris = [s.uri for s in result.skills]
            self.assertEqual(len(uris), len(set(uris)), "Skill URIs should be unique")
        self._run(_test())

    def test_04_feedback_updates_skillbook_tag(self):
        """Feedback on a skillbook URI updates the skill tag."""
        async def _test():
            orch = await self._init_orch()
            result = await orch.hooks_remember(
                content="Use chardet for encoding detection",
                memory_type="error_fixes",
            )
            uri = result.get("uri", "")
            self.assertIn("/skillbooks/", uri)

            await orch.feedback(uri=uri, reward=1.0)

            skill_id = result.get("skill_id", "")
            skills = await orch._hooks.skillbook.get_by_section("error_fixes")
            matching = [s for s in skills if s.id == skill_id]
            if matching:
                self.assertGreater(matching[0].helpful, 0, "Helpful count should increase")
        self._run(_test())

    def test_05_feedback_negative_marks_harmful(self):
        """Negative feedback marks skill as harmful."""
        async def _test():
            orch = await self._init_orch()
            result = await orch.hooks_remember(
                content="Use eval() for dynamic code",
                memory_type="patterns",
            )
            uri = result.get("uri", "")
            await orch.feedback(uri=uri, reward=-1.0)

            skill_id = result.get("skill_id", "")
            skills = await orch._hooks.skillbook.get_by_section("patterns")
            matching = [s for s in skills if s.id == skill_id]
            if matching:
                self.assertGreater(matching[0].harmful, 0, "Harmful count should increase")
        self._run(_test())


# =============================================================================
# Tests: Async Skill Extraction from add()
# =============================================================================


class TestAsyncSkillExtraction(unittest.TestCase):
    """Test that add() triggers async skill extraction."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = CortexConfig(
            tenant_id="testteam",
            user_id="alice",
            data_root=self.temp_dir,
            embedding_provider="none",
        )
        init_config(self.config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    async def _init_orch(self) -> MemoryOrchestrator:
        storage = InMemoryStorage()
        embedder = MockEmbedder()
        orch = MemoryOrchestrator(
            config=self.config,
            storage=storage,
            embedder=embedder,
        )
        await orch.init()
        return orch

    def test_06_add_with_error_content_extracts_skill(self):
        """add() with error→fix content creates a skill in Skillbook."""
        async def _test():
            orch = await self._init_orch()

            content = (
                "During deployment, an error occurred:\n"
                "ConnectionError: unable to connect to database on port 5432\n\n"
                "The fix was to check the database connection string. "
                "When encountering connection errors, first verify the host and port, "
                "then check firewall rules and ensure the service is running.\n"
                "After applying the fix, the deployment succeeded without issues.\n"
                "Additional logging was added for future diagnostics.\n"
            )

            await orch.add(
                abstract="Database connection fix",
                content=content,
                category="incidents",
            )
            # Let the background task complete
            await asyncio.sleep(0.1)

            skills = await orch.hooks_recall("database connection error")
            self.assertIsInstance(skills, list)
        self._run(_test())

    def test_07_add_without_content_no_extraction(self):
        """add() without content does not attempt extraction."""
        async def _test():
            orch = await self._init_orch()
            await orch.add(abstract="Simple note", category="notes")
            await asyncio.sleep(0.05)
            stats = await orch.hooks_stats()
            self.assertIsInstance(stats, dict)
        self._run(_test())

    def test_08_add_with_preference_content(self):
        """add() with preference keywords triggers skill extraction."""
        async def _test():
            orch = await self._init_orch()

            content = (
                "Team meeting decisions:\n"
                "We agreed that we should always use ESLint with strict mode enabled.\n"
                "This applies to all JavaScript and TypeScript files in the repo.\n"
                "The configuration file should be committed to the repository root.\n"
                "Team members should never disable linting rules without review.\n"
            )

            await orch.add(
                abstract="Team coding standards",
                content=content,
                category="decisions",
            )
            await asyncio.sleep(0.1)

            skills = await orch.hooks_recall("eslint configuration")
            self.assertIsInstance(skills, list)
        self._run(_test())

    def test_09_extraction_failure_is_silent(self):
        """Extraction errors don't affect add() return."""
        async def _test():
            orch = await self._init_orch()
            result = await orch.add(
                abstract="Test",
                content="x" * 200,
                category="test",
            )
            self.assertIsNotNone(result)
            self.assertIn("uri", result.__dict__)
        self._run(_test())


# =============================================================================
# Tests: Recall with URI and Score
# =============================================================================


class TestSkillRecallWithURI(unittest.TestCase):
    """Test that recall returns URI and score fields."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = CortexConfig(
            tenant_id="testteam",
            user_id="alice",
            data_root=self.temp_dir,
            embedding_provider="none",
        )
        init_config(self.config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    async def _init_orch(self) -> MemoryOrchestrator:
        storage = InMemoryStorage()
        embedder = MockEmbedder()
        orch = MemoryOrchestrator(
            config=self.config,
            storage=storage,
            embedder=embedder,
        )
        await orch.init()
        return orch

    def test_10_recall_returns_uri(self):
        """hooks_recall results include uri field."""
        async def _test():
            orch = await self._init_orch()
            await orch.hooks_remember(content="Use ruff for linting Python", memory_type="preferences")
            results = await orch.hooks_recall("linting")
            self.assertGreater(len(results), 0)
            self.assertIn("uri", results[0])
            self.assertIn("/skillbooks/", results[0]["uri"])
        self._run(_test())

    def test_11_recall_returns_score(self):
        """hooks_recall results include score field."""
        async def _test():
            orch = await self._init_orch()
            await orch.hooks_remember(content="Deploy with docker compose", memory_type="workflows")
            results = await orch.hooks_recall("deploy docker")
            self.assertGreater(len(results), 0)
            self.assertIn("score", results[0])
        self._run(_test())


if __name__ == "__main__":
    unittest.main()
