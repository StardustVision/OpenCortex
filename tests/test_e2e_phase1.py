"""
End-to-end Phase 1 validation for OpenCortex.

Tests the complete pipeline: config -> orchestrator -> add -> embed ->
vector store + filesystem -> search -> feedback -> decay -> remove.

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
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.core.context import Context
from opencortex.core.message import Message
from opencortex.core.user_id import UserIdentifier
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator
from opencortex.retrieve.rerank_config import RerankConfig
from opencortex.retrieve.types import ContextType, FindResult
from opencortex.storage.vikingdb_interface import (
    CollectionNotFoundError,
    VikingDBInterface,
)
from opencortex.utils.uri import CortexURI


class TestContextTypeEnum(unittest.TestCase):
    def test_new_context_types_exist(self):
        from opencortex.retrieve.types import ContextType
        self.assertEqual(ContextType.CASE.value, "case")
        self.assertEqual(ContextType.PATTERN.value, "pattern")
        self.assertEqual(ContextType.STAGING.value, "staging")

    def test_legacy_context_types_unchanged(self):
        from opencortex.retrieve.types import ContextType
        self.assertEqual(ContextType.MEMORY.value, "memory")
        self.assertEqual(ContextType.RESOURCE.value, "resource")
        self.assertEqual(ContextType.SKILL.value, "skill")


# =============================================================================
# Mock Embedder
# =============================================================================


class MockEmbedder(DenseEmbedderBase):
    """Deterministic embedder for testing.

    Produces a 4-dimensional vector based on simple text hashing so that
    similar texts produce somewhat similar vectors.
    """

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
        """Hash-based deterministic vector."""
        h = hash(text) & 0xFFFFFFFF
        raw = [
            ((h >> 0) & 0xFF) / 255.0,
            ((h >> 8) & 0xFF) / 255.0,
            ((h >> 16) & 0xFF) / 255.0,
            ((h >> 24) & 0xFF) / 255.0,
        ]
        # L2 normalize
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


# =============================================================================
# In-Memory Storage Backend
# =============================================================================


class InMemoryStorage(VikingDBInterface):
    """Fully in-memory VikingDBInterface implementation with RL support.

    Stores records as dicts in a nested {collection: {id: record}} structure.
    Supports cosine similarity search and basic filter evaluation.
    Also provides reinforcement learning methods (update_reward, get_profile,
    apply_decay, set_protected).
    """

    def __init__(self):
        self._collections: Dict[str, Dict[str, Any]] = {}  # name -> schema
        self._records: Dict[str, Dict[str, Dict[str, Any]]] = {}  # col -> id -> record
        self._rl_profiles: Dict[str, Dict[str, Any]] = {}  # col::id -> profile
        self._closed = False

    # ---- Collection Management ----

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

    # ---- Single CRUD ----

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

    # ---- Batch CRUD ----

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

    # ---- Search ----

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
        text_query: str = "",
    ) -> List[Dict[str, Any]]:
        self._ensure(collection)
        candidates = list(self._records[collection].values())

        # Apply filter
        if filter:
            candidates = [r for r in candidates if self._eval_filter(r, filter)]

        # Score by cosine similarity
        if query_vector:
            scored = []
            for r in candidates:
                vec = r.get("vector")
                if vec:
                    score = self._cosine_sim(query_vector, vec)
                else:
                    score = 0.0
                rec = dict(r)
                rec["_score"] = score
                scored.append(rec)
            scored.sort(key=lambda x: x["_score"], reverse=True)
            candidates = scored
        else:
            for r in candidates:
                r = dict(r)

        # Text fallback when no vector scoring was applied
        if not query_vector and text_query:
            query_lower = text_query.lower()
            scored = []
            for r in (candidates if isinstance(candidates, list) else list(candidates)):
                r = dict(r)
                abstract = (r.get("abstract") or "").lower()
                overview = (r.get("overview") or "").lower()
                score = 0.0
                if query_lower in abstract:
                    score += 0.8
                if query_lower in overview:
                    score += 0.4
                r["_score"] = score
                scored.append(r)
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

    # ---- Aggregation ----

    async def count(self, collection: str, filter: Optional[Dict[str, Any]] = None) -> int:
        self._ensure(collection)
        if filter:
            return len(await self.filter(collection, filter, limit=100_000))
        return len(self._records[collection])

    # ---- Index (no-op) ----

    async def create_index(self, collection: str, field: str, index_type: str, **kw) -> bool:
        return True

    async def drop_index(self, collection: str, field: str) -> bool:
        return True

    # ---- Lifecycle ----

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

    # ---- Reinforcement Learning ----

    async def update_reward(self, collection: str, id: str, reward: float) -> None:
        self._ensure(collection)
        record = self._records[collection].get(id)
        if record is None:
            return
        record["reward_score"] = record.get("reward_score", 0.0) + reward
        pos = record.get("positive_feedback_count", 0)
        neg = record.get("negative_feedback_count", 0)
        if reward > 0:
            pos += 1
        elif reward < 0:
            neg += 1
        record["positive_feedback_count"] = pos
        record["negative_feedback_count"] = neg
        record.setdefault("active_count", 0)

    async def update_reward_batch(
        self, collection: str, rewards: List[Tuple[str, float]]
    ) -> None:
        for rid, reward in rewards:
            await self.update_reward(collection, rid, reward)

    async def get_profile(self, collection: str, id: str):
        self._ensure(collection)
        record = self._records[collection].get(id)
        if record is None:
            return None
        return _SimpleProfile(
            id=id,
            reward_score=record.get("reward_score", 0.0),
            retrieval_count=record.get("active_count", 0),
            positive_feedback_count=record.get("positive_feedback_count", 0),
            negative_feedback_count=record.get("negative_feedback_count", 0),
            effective_score=record.get("reward_score", 0.0),
            is_protected=record.get("protected", False),
            accessed_at=record.get("accessed_at", ""),
        )

    async def apply_decay(self, decay_rate=0.95, protected_rate=0.99, threshold=0.01):
        import math as _math
        from datetime import datetime, timezone

        processed = decayed = below = 0
        now = datetime.now(timezone.utc)
        for col_records in self._records.values():
            for record in col_records.values():
                processed += 1
                reward = record.get("reward_score", 0.0)
                if reward == 0.0:
                    continue
                is_protected = record.get("protected", False)
                rate = protected_rate if is_protected else decay_rate
                # Access-driven protection
                accessed_at = record.get("accessed_at")
                if accessed_at:
                    try:
                        accessed_dt = datetime.fromisoformat(
                            accessed_at.replace("Z", "+00:00")
                        )
                        days_since = max(0, (now - accessed_dt).days)
                        access_bonus = 0.04 * _math.exp(-days_since / 30)
                        rate = min(1.0, rate + access_bonus)
                    except (ValueError, TypeError):
                        pass
                new_reward = reward * rate
                if abs(new_reward) < threshold:
                    new_reward = 0.0
                    below += 1
                record["reward_score"] = new_reward
                decayed += 1

        class _R:
            def __init__(self):
                self.records_processed = processed
                self.records_decayed = decayed
                self.records_below_threshold = below
                self.records_archived = 0

        return _R()

    async def set_protected(self, collection: str, id: str, protected: bool = True) -> None:
        self._ensure(collection)
        record = self._records[collection].get(id)
        if record is not None:
            record["protected"] = protected

    # ---- Internal helpers ----

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
        """Evaluate VikingDB-style filter DSL against a record."""
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

        elif op == "contains":
            field_name = filt.get("field", "")
            substring = filt.get("substring", "")
            val = str(record.get(field_name, ""))
            return substring in val

        elif op == "and":
            return all(self._eval_filter(record, c) for c in filt.get("conds", []))

        elif op == "or":
            return any(self._eval_filter(record, c) for c in filt.get("conds", []))

        return True


@dataclass
class _SimpleProfile:
    """Duck-type SonaProfile for testing."""

    id: str = ""
    reward_score: float = 0.0
    retrieval_count: int = 0
    positive_feedback_count: int = 0
    negative_feedback_count: int = 0
    last_retrieved_at: float = 0.0
    last_feedback_at: float = 0.0
    effective_score: float = 0.0
    is_protected: bool = False
    accessed_at: str = ""


@dataclass
class _SimpleDecayResult:
    """Duck-type DecayResult for testing."""

    records_processed: int = 0
    records_decayed: int = 0
    records_below_threshold: int = 0
    records_archived: int = 0


# =============================================================================
# E2E Test Suite
# =============================================================================


class TestE2EPhase1(unittest.TestCase):
    """End-to-end validation of the complete OpenCortex Phase 1 pipeline."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="opencortex_e2e_")
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
        """Run an async coroutine."""
        return asyncio.run(coro)

    # -----------------------------------------------------------------
    # 1. Initialization
    # -----------------------------------------------------------------

    def test_01_init(self):
        """Orchestrator initializes all components correctly."""
        orch = MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=self.embedder,
        )

        # Before init
        health = self._run(orch.health_check())
        self.assertFalse(health["initialized"])

        # Init
        result = self._run(orch.init())
        self.assertIs(result, orch)

        # After init
        health = self._run(orch.health_check())
        self.assertTrue(health["initialized"])
        self.assertTrue(health["storage"])
        self.assertTrue(health["embedder"])

        # Collection should be created
        self.assertTrue(self._run(self.storage.collection_exists("context")))

        # Stats
        stats = self._run(orch.stats())
        self.assertEqual(stats["tenant_id"], "testteam")
        self.assertEqual(stats["user_id"], "alice")
        self.assertEqual(stats["storage"]["backend"], "in-memory")

    # -----------------------------------------------------------------
    # 2. Add Memories
    # -----------------------------------------------------------------

    def test_02_add_memory(self):
        """Add a memory with auto-generated URI and verify storage."""
        orch = self._init_orch()

        ctx = self._run(
            orch.add(
                abstract="User prefers dark theme in all editors",
                content="# Theme Preference\nDark theme everywhere.",
                category="preferences",
            )
        )

        # Verify Context object
        self.assertIsInstance(ctx, Context)
        self.assertIn("memories", ctx.uri)
        self.assertIn("preferences", ctx.uri)
        self.assertIn("testteam", ctx.uri)
        self.assertIn("alice", ctx.uri)
        self.assertEqual(ctx.context_type, "memory")
        self.assertEqual(ctx.category, "preferences")
        self.assertTrue(ctx.is_leaf)
        self.assertIsNotNone(ctx.vector)
        self.assertEqual(len(ctx.vector), MockEmbedder.DIMENSION)

        # Verify URI is valid
        parsed = CortexURI(ctx.uri)
        self.assertEqual(parsed.tenant_id, "testteam")
        self.assertEqual(parsed.user_id, "alice")
        self.assertTrue(parsed.is_private)

        # Verify record in vector DB
        records = self._run(
            self.storage.filter(
                "context",
                {"op": "must", "field": "uri", "conds": [ctx.uri]},
                limit=1,
            )
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["abstract"], "User prefers dark theme in all editors")

        # Verify filesystem (L0 abstract written)
        abstract_path = os.path.join(
            self.temp_dir,
            "testteam", "user", "alice", "memories", "preferences",
            ctx.uri.split("/")[-1],
            ".abstract.md",
        )
        self.assertTrue(os.path.exists(abstract_path), f"Abstract file should exist at {abstract_path}")
        with open(abstract_path, "r") as f:
            self.assertEqual(f.read(), "User prefers dark theme in all editors")

    def test_03_add_resource(self):
        """Add a shared resource and verify it's team-level."""
        orch = self._init_orch()

        ctx = self._run(
            orch.add(
                abstract="Python coding standards for the team",
                context_type="resource",
                category="standards",
            )
        )

        self.assertIn("resources", ctx.uri)
        self.assertIn("standards", ctx.uri)

        parsed = CortexURI(ctx.uri)
        self.assertTrue(parsed.is_shared)
        self.assertEqual(parsed.tenant_id, "testteam")

    def test_04_add_skill(self):
        """Add a shared skill and verify structure."""
        orch = self._init_orch()

        ctx = self._run(
            orch.add(
                abstract="Convert Word documents to Markdown",
                context_type="skill",
                meta={"name": "word_to_md", "description": "Word to Markdown converter"},
            )
        )

        self.assertIn("/shared/skills/", ctx.uri)
        self.assertEqual(ctx.context_type, "skill")

        parsed = CortexURI(ctx.uri)
        self.assertTrue(parsed.is_shared)

    # -----------------------------------------------------------------
    # 3. Search
    # -----------------------------------------------------------------

    def test_05_search_basic(self):
        """Basic search returns relevant memories."""
        orch = self._init_orch()

        # Add multiple contexts
        self._run(orch.add(abstract="User prefers dark theme", category="preferences"))
        self._run(orch.add(abstract="User likes Python over Java", category="preferences"))
        self._run(orch.add(abstract="Project deadline is March 15", category="events"))
        self._run(
            orch.add(
                abstract="REST API design guidelines",
                context_type="resource",
                category="docs",
            )
        )

        # Search for memories
        result = self._run(orch.search("What theme does the user prefer?"))
        self.assertIsInstance(result, FindResult)

        # Should find some results (exact count depends on vector similarity)
        total = result.total
        self.assertGreater(total, 0, "Should find at least one result")

    def test_06_search_by_type(self):
        """Search restricted to a specific context type."""
        orch = self._init_orch()

        self._run(orch.add(abstract="Dark theme preference", category="preferences"))
        self._run(
            orch.add(
                abstract="API versioning best practices",
                context_type="resource",
                category="docs",
            )
        )

        # Search only resources
        result = self._run(
            orch.search("API practices", context_type=ContextType.RESOURCE)
        )

        # All results should be resources
        for ctx in result.resources:
            self.assertEqual(ctx.context_type, ContextType.RESOURCE)

        # No memories expected
        self.assertEqual(len(result.memories), 0)

    # -----------------------------------------------------------------
    # 4. Reinforcement Learning
    # -----------------------------------------------------------------

    def test_07_feedback(self):
        """Feedback sends reward signal and updates activity count."""
        orch = self._init_orch()

        ctx = self._run(orch.add(abstract="Important design decision: use microservices"))

        # Send positive feedback
        self._run(orch.feedback(ctx.uri, reward=1.0))

        # Verify activity count updated
        records = self._run(
            self.storage.filter(
                "context",
                {"op": "must", "field": "uri", "conds": [ctx.uri]},
                limit=1,
            )
        )
        self.assertEqual(records[0].get("active_count"), 1)

        # Verify RL profile
        profile = self._run(orch.get_profile(ctx.uri))
        self.assertIsNotNone(profile)
        self.assertEqual(profile["reward_score"], 1.0)
        self.assertEqual(profile["positive_feedback_count"], 1)

    def test_08_feedback_negative(self):
        """Negative feedback decreases reward score."""
        orch = self._init_orch()

        ctx = self._run(orch.add(abstract="Outdated info: use monolith"))

        self._run(orch.feedback(ctx.uri, reward=-0.5))

        profile = self._run(orch.get_profile(ctx.uri))
        self.assertIsNotNone(profile)
        self.assertEqual(profile["reward_score"], -0.5)
        self.assertEqual(profile["negative_feedback_count"], 1)

    def test_09_feedback_batch(self):
        """Batch feedback updates multiple contexts."""
        orch = self._init_orch()

        ctx1 = self._run(orch.add(abstract="Good memory A"))
        ctx2 = self._run(orch.add(abstract="Bad memory B"))

        self._run(
            orch.feedback_batch(
                [
                    {"uri": ctx1.uri, "reward": 1.0},
                    {"uri": ctx2.uri, "reward": -1.0},
                ]
            )
        )

        p1 = self._run(orch.get_profile(ctx1.uri))
        p2 = self._run(orch.get_profile(ctx2.uri))
        self.assertEqual(p1["reward_score"], 1.0)
        self.assertEqual(p2["reward_score"], -1.0)

    # -----------------------------------------------------------------
    # 5. Decay
    # -----------------------------------------------------------------

    def test_10_decay(self):
        """Time-decay reduces effective scores."""
        orch = self._init_orch()

        ctx = self._run(orch.add(abstract="Decaying memory"))
        self._run(orch.feedback(ctx.uri, reward=10.0))

        profile_before = self._run(orch.get_profile(ctx.uri))
        self.assertEqual(profile_before["effective_score"], 10.0)

        # Apply decay
        decay_result = self._run(orch.decay())
        self.assertIsNotNone(decay_result)
        self.assertGreater(decay_result["records_processed"], 0)

        profile_after = self._run(orch.get_profile(ctx.uri))
        self.assertLess(
            profile_after["effective_score"],
            profile_before["effective_score"],
            "Effective score should decrease after decay",
        )

    def test_11_protect_slows_decay(self):
        """Protected contexts decay slower than unprotected ones."""
        orch = self._init_orch()

        ctx_normal = self._run(orch.add(abstract="Normal memory"))
        ctx_protected = self._run(orch.add(abstract="Protected memory"))

        self._run(orch.feedback(ctx_normal.uri, reward=10.0))
        self._run(orch.feedback(ctx_protected.uri, reward=10.0))

        # Protect one
        self._run(orch.protect(ctx_protected.uri, protected=True))

        # Apply decay
        self._run(orch.decay())

        p_normal = self._run(orch.get_profile(ctx_normal.uri))
        p_protected = self._run(orch.get_profile(ctx_protected.uri))

        # Protected should retain more (0.99 vs 0.95 decay rate)
        self.assertGreater(
            p_protected["effective_score"],
            p_normal["effective_score"],
            "Protected memory should decay slower",
        )

    # -----------------------------------------------------------------
    # 6. Update
    # -----------------------------------------------------------------

    def test_12_update(self):
        """Update modifies abstract and re-embeds."""
        orch = self._init_orch()

        ctx = self._run(orch.add(abstract="Original abstract"))
        original_uri = ctx.uri

        success = self._run(orch.update(original_uri, abstract="Updated abstract"))
        self.assertTrue(success)

        records = self._run(
            self.storage.filter(
                "context",
                {"op": "must", "field": "uri", "conds": [original_uri]},
                limit=1,
            )
        )
        self.assertEqual(records[0]["abstract"], "Updated abstract")

    def test_13_update_not_found(self):
        """Update returns False for non-existent URI."""
        orch = self._init_orch()

        success = self._run(orch.update("opencortex://testteam/user/alice/memories/nonexistent", abstract="x"))
        self.assertFalse(success)

    # -----------------------------------------------------------------
    # 7. Remove
    # -----------------------------------------------------------------

    def test_14_remove(self):
        """Remove deletes from vector DB and filesystem."""
        orch = self._init_orch()

        ctx = self._run(orch.add(abstract="To be removed", content="Bye"))

        # Verify exists
        count_before = self._run(self.storage.count("context"))
        self.assertGreater(count_before, 0)

        # Remove
        removed = self._run(orch.remove(ctx.uri))
        self.assertGreater(removed, 0)

        # Verify gone from vector DB
        records = self._run(
            self.storage.filter(
                "context",
                {"op": "must", "field": "uri", "conds": [ctx.uri]},
                limit=1,
            )
        )
        self.assertEqual(len(records), 0)

    # -----------------------------------------------------------------
    # 8. Tenant / User Isolation
    # -----------------------------------------------------------------

    def test_15_tenant_isolation(self):
        """Different tenants produce different URI prefixes."""
        config = CortexConfig(data_root=self.temp_dir)

        storage = InMemoryStorage()
        embedder = MockEmbedder()

        orch = self._run(
            MemoryOrchestrator(config=config, storage=storage, embedder=embedder).init()
        )

        tokens_a = set_request_identity("teamA", "alice")
        try:
            ctx_a = self._run(orch.add(abstract="Team A memory"))
        finally:
            reset_request_identity(tokens_a)

        tokens_b = set_request_identity("teamB", "bob")
        try:
            ctx_b = self._run(orch.add(abstract="Team B memory"))
        finally:
            reset_request_identity(tokens_b)

        self.assertIn("teamA", ctx_a.uri)
        self.assertIn("alice", ctx_a.uri)
        self.assertIn("teamB", ctx_b.uri)
        self.assertIn("bob", ctx_b.uri)

        # Different URI prefixes
        self.assertNotEqual(
            CortexURI(ctx_a.uri).tenant_id,
            CortexURI(ctx_b.uri).tenant_id,
        )

    # -----------------------------------------------------------------
    # 9. UserIdentifier Integration
    # -----------------------------------------------------------------

    def test_16_user_identifier(self):
        """UserIdentifier produces correct tenant-based URIs."""
        user = UserIdentifier("myteam", "bob", "assistant")

        mem_uri = user.memory_space_uri()
        self.assertEqual(mem_uri, "opencortex://myteam/user/bob/memories")

        cases_uri = user.agent_cases_uri()
        self.assertEqual(cases_uri, "opencortex://myteam/user/bob/agent/memories/cases")

        ws_uri = user.workspace_uri("proj1")
        self.assertEqual(ws_uri, "opencortex://myteam/user/bob/workspace/proj1")

    # -----------------------------------------------------------------
    # 10. Context Type Derivation
    # -----------------------------------------------------------------

    def test_17_context_type_derivation(self):
        """Context correctly derives type and category from URI."""
        mem_ctx = Context(uri="opencortex://t1/user/u1/memories/preferences/node1")
        self.assertEqual(mem_ctx.context_type, "memory")
        self.assertEqual(mem_ctx.category, "preferences")

        skill_ctx = Context(uri="opencortex://t1/agent/skills/convert_doc")
        self.assertEqual(skill_ctx.context_type, "skill")

        res_ctx = Context(uri="opencortex://t1/resources/docs/api_guide")
        self.assertEqual(res_ctx.context_type, "resource")

        pattern_ctx = Context(uri="opencortex://t1/agent/memories/patterns/retry")
        self.assertEqual(pattern_ctx.context_type, "memory")
        self.assertEqual(pattern_ctx.category, "patterns")

        cases_ctx = Context(uri="opencortex://t1/user/u1/agent/memories/cases/c1")
        self.assertEqual(cases_ctx.context_type, "memory")
        self.assertEqual(cases_ctx.category, "cases")

    # -----------------------------------------------------------------
    # 11. CortexURI Builders
    # -----------------------------------------------------------------

    def test_18_uri_builders(self):
        """CortexURI builders produce valid, parseable URIs."""
        shared = CortexURI.build_shared("t1", "resources", "docs")
        self.assertEqual(shared, "opencortex://t1/resources/docs")

        private = CortexURI.build_private("t1", "u1", "memories", "events")
        self.assertEqual(private, "opencortex://t1/user/u1/memories/events")

        parsed = CortexURI(private)
        self.assertEqual(parsed.tenant_id, "t1")
        self.assertEqual(parsed.user_id, "u1")
        self.assertEqual(parsed.sub_scope, "memories")
        self.assertTrue(parsed.is_private)

    # -----------------------------------------------------------------
    # 12. CortexFS Filesystem Layer
    # -----------------------------------------------------------------

    def test_19_vikingfs_write_and_read(self):
        """CortexFS correctly writes and reads L0/L1/L2 content."""
        orch = self._init_orch()

        uri = CortexURI.build_private("testteam", "alice", "memories", "test_node")

        # Write context with all layers
        self._run(
            orch.fs.write_context(
                uri=uri,
                content="# Full Content\nDetailed explanation here.",
                abstract="Short summary of the content",
                overview="Medium-length overview with more details",
            )
        )

        # Read back
        abstract = self._run(orch.fs.abstract(uri))
        self.assertEqual(abstract, "Short summary of the content")

        overview = self._run(orch.fs.overview(uri))
        self.assertEqual(overview, "Medium-length overview with more details")

        content = self._run(orch.fs.read_file(f"{uri}/content.md"))
        self.assertIn("Full Content", content)

    # -----------------------------------------------------------------
    # 13. Collection Schemas
    # -----------------------------------------------------------------

    def test_20_collection_schema(self):
        """Context collection is created with correct schema."""
        orch = self._init_orch()

        info = self._run(self.storage.get_collection_info("context"))
        self.assertIsNotNone(info)
        self.assertEqual(info["name"], "context")
        self.assertEqual(info["status"], "ready")

    # -----------------------------------------------------------------
    # 14. Full Pipeline Integration
    # -----------------------------------------------------------------

    def test_21_full_pipeline(self):
        """Complete pipeline: add -> search -> feedback -> decay -> verify."""
        orch = self._init_orch()

        # Step 1: Add multiple memories
        memories = []
        for text in [
            "User prefers dark theme in VS Code",
            "User works on Python microservices",
            "Team uses PostgreSQL for production",
            "Deploy process: CI/CD via GitHub Actions",
        ]:
            ctx = self._run(orch.add(abstract=text, category="preferences"))
            memories.append(ctx)

        # Step 2: Add a resource
        resource = self._run(
            orch.add(
                abstract="Python style guide with PEP8 conventions",
                context_type="resource",
                category="standards",
            )
        )

        # Verify count (5 leaf nodes + directory nodes created by _ensure_parent_records)
        stats = self._run(orch.stats())
        self.assertGreaterEqual(stats["storage"]["total_records"], 5)

        # Step 3: Search
        result = self._run(orch.search("Python development practices"))
        self.assertGreater(result.total, 0)

        # Step 4: Feedback on first result
        all_results = list(result)
        if all_results:
            first = all_results[0]
            self._run(orch.feedback(first.uri, reward=1.0))

            # Verify profile
            profile = self._run(orch.get_profile(first.uri))
            self.assertIsNotNone(profile)
            self.assertEqual(profile["positive_feedback_count"], 1)

        # Step 5: Protect important memory
        self._run(orch.protect(memories[0].uri))

        # Step 6: Decay
        decay_result = self._run(orch.decay())
        if decay_result:
            self.assertGreaterEqual(decay_result["records_processed"], 0)

        # Step 7: Update a memory
        self._run(orch.update(memories[0].uri, abstract="User STRONGLY prefers dark theme"))
        records = self._run(
            self.storage.filter(
                "context",
                {"op": "must", "field": "uri", "conds": [memories[0].uri]},
                limit=1,
            )
        )
        self.assertEqual(records[0]["abstract"], "User STRONGLY prefers dark theme")

        # Step 8: Remove one memory
        records_before = (self._run(orch.stats()))["storage"]["total_records"]
        self._run(orch.remove(memories[-1].uri))
        records_after = (self._run(orch.stats()))["storage"]["total_records"]
        self.assertLess(records_after, records_before)

        # Step 9: Close
        self._run(orch.close())
        health = self._run(orch.health_check())
        self.assertFalse(health["initialized"])

    # -----------------------------------------------------------------
    # 15. Idempotent Init
    # -----------------------------------------------------------------

    def test_22_idempotent_init(self):
        """Calling init() twice is safe."""
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder
        )
        self._run(orch.init())
        self._run(orch.init())  # Should be no-op

        health = self._run(orch.health_check())
        self.assertTrue(health["initialized"])

    # -----------------------------------------------------------------
    # 16. Error Handling
    # -----------------------------------------------------------------

    def test_23_search_before_init_raises(self):
        """Operations before init() raise RuntimeError."""
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder
        )
        with self.assertRaises(RuntimeError):
            self._run(orch.search("test"))

    def test_24_feedback_nonexistent_uri(self):
        """Feedback on non-existent URI is a no-op (no crash)."""
        orch = self._init_orch()
        # Should not raise
        self._run(orch.feedback("opencortex://testteam/user/alice/memories/ghost", 1.0))

    # -----------------------------------------------------------------
    # Helper
    # -----------------------------------------------------------------

    def _init_orch(self) -> MemoryOrchestrator:
        """Create and initialize an orchestrator for testing."""
        orch = MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=self.embedder,
        )
        self._run(orch.init())
        return orch


class TestAutoUri(unittest.TestCase):
    """Test _auto_uri routing table."""

    def setUp(self):
        from opencortex.http.request_context import set_request_identity
        self._token = set_request_identity("testteam", "alice")

    def tearDown(self):
        from opencortex.http.request_context import reset_request_identity
        reset_request_identity(self._token)

    def _auto_uri(self, context_type, category):
        from opencortex.orchestrator import MemoryOrchestrator
        o = MemoryOrchestrator.__new__(MemoryOrchestrator)
        return o._auto_uri(context_type, category)

    def test_memory_profile(self):
        uri = self._auto_uri("memory", "profile")
        self.assertIn("/user/alice/memories/profile/", uri)
        self.assertTrue(uri.startswith("opencortex://testteam/"))

    def test_memory_preferences(self):
        uri = self._auto_uri("memory", "preferences")
        self.assertIn("/user/alice/memories/preferences/", uri)

    def test_memory_empty_category_defaults_to_events(self):
        uri = self._auto_uri("memory", "")
        self.assertIn("/user/alice/memories/events/", uri)

    def test_case(self):
        uri = self._auto_uri("case", "anything")
        self.assertIn("/shared/cases/", uri)
        self.assertNotIn("/user/", uri)

    def test_pattern(self):
        uri = self._auto_uri("pattern", "")
        self.assertIn("/shared/patterns/", uri)

    def test_skill_error_fixes(self):
        uri = self._auto_uri("skill", "error_fixes")
        self.assertIn("/shared/skills/error_fixes/", uri)

    def test_skill_empty_defaults_to_general(self):
        uri = self._auto_uri("skill", "")
        self.assertIn("/shared/skills/general/", uri)

    def test_resource_documents(self):
        uri = self._auto_uri("resource", "documents")
        self.assertIn("/resources/documents/", uri)

    def test_staging(self):
        uri = self._auto_uri("staging", "")
        self.assertIn("/user/alice/staging/", uri)


class TestAddScopeFields(unittest.TestCase):
    """Test that add() populates scope/category/source fields in Qdrant."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def test_add_memory_sets_private_scope(self):
        from opencortex.http.request_context import set_request_identity, reset_request_identity
        token = set_request_identity("t1", "u1")
        try:
            storage = InMemoryStorage()
            orch = self._build_orch(storage)
            ctx = self.loop.run_until_complete(
                orch.add(abstract="test pref", category="preferences", context_type="memory")
            )
            # Check Qdrant record has scope fields
            records = self.loop.run_until_complete(
                storage.filter("context",
                    {"op": "must", "field": "uri", "conds": [ctx.uri]}, limit=1)
            )
            self.assertEqual(records[0].get("scope"), "private")
            self.assertEqual(records[0].get("category"), "preferences")
            self.assertEqual(records[0].get("source_user_id"), "u1")
            self.assertTrue(records[0].get("mergeable"))
        finally:
            reset_request_identity(token)

    def test_add_case_sets_shared_scope(self):
        from opencortex.http.request_context import set_request_identity, reset_request_identity
        token = set_request_identity("t1", "u1")
        try:
            storage = InMemoryStorage()
            orch = self._build_orch(storage)
            ctx = self.loop.run_until_complete(
                orch.add(abstract="bug fix", context_type="case")
            )
            records = self.loop.run_until_complete(
                storage.filter("context",
                    {"op": "must", "field": "uri", "conds": [ctx.uri]}, limit=1)
            )
            self.assertEqual(records[0].get("scope"), "shared")
            self.assertFalse(records[0].get("mergeable"))
        finally:
            reset_request_identity(token)

    def _build_orch(self, storage):
        """Create a minimal orchestrator with the given storage."""
        from opencortex.orchestrator import MemoryOrchestrator
        from opencortex.config import CortexConfig
        cfg = CortexConfig(embedding_provider="none")
        orch = MemoryOrchestrator(config=cfg)
        orch._storage = storage
        orch._embedder = MockEmbedder()
        orch._initialized = True
        # Create the context collection so upsert/filter work
        self.loop.run_until_complete(
            storage.create_collection("context", {"vector_dim": MockEmbedder.DIMENSION})
        )
        # Initialize CortexFS for write_context
        from opencortex.storage.cortex_fs import CortexFS
        import tempfile
        orch._fs = CortexFS(data_root=tempfile.mkdtemp())
        return orch


class TestAcePreferencesRouting(unittest.TestCase):
    """ACE-extracted preferences should route to user/memories, not shared/skills."""

    def test_preferences_section_routes_to_user_memory(self):
        """When RuleExtractor extracts a preferences skill, it should be stored
        as a user memory, not as a shared skill."""
        from opencortex.ace.engine import ACEngine
        # The remember() method should detect preferences section
        # and route to user memory instead of skillbook
        # This test validates the routing decision
        self.assertIn("preferences", ACEngine._USER_MEMORY_SECTIONS)

    def test_error_fixes_routes_to_shared(self):
        """error_fixes should NOT be in _USER_MEMORY_SECTIONS — they stay shared."""
        from opencortex.ace.engine import ACEngine
        self.assertNotIn("error_fixes", ACEngine._USER_MEMORY_SECTIONS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
