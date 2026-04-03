"""
End-to-end tests for the Skill Engine pipeline.

Tests the complete flow: store memories → extract skills → approve → recall.
Uses in-memory mocks (no external binary or network calls needed).

Reuses MockEmbedder and InMemoryStorage from test_e2e_phase1.
"""

import asyncio
import math
import os
import sys
import unittest
from typing import Any, Dict, List, Optional
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from opencortex.http.request_context import (
    set_request_identity, reset_request_identity,
)
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, SkillVisibility, SkillOrigin,
    SkillLineage, EvolutionSuggestion,
    make_skill_uri, make_source_fingerprint,
)
from opencortex.skill_engine.adapters.storage_adapter import SkillStorageAdapter
from opencortex.skill_engine.adapters.source_adapter import (
    QdrantSourceAdapter, MemoryCluster, MemoryRecord,
)
from opencortex.skill_engine.adapters.llm_adapter import LLMCompletionAdapter
from opencortex.skill_engine.store import SkillStore
from opencortex.skill_engine.analyzer import SkillAnalyzer
from opencortex.skill_engine.evolver import SkillEvolver
from opencortex.skill_engine.skill_manager import SkillManager
from opencortex.skill_engine.ranker import SkillRanker


# =============================================================================
# Mocks (same patterns as test_e2e_phase1)
# =============================================================================

class MockEmbedder(DenseEmbedderBase):
    """Deterministic embedder producing 4D vectors from text hash."""
    DIMENSION = 4

    def __init__(self):
        super().__init__(model_name="mock-embedder-v1")

    def embed(self, text: str) -> EmbedResult:
        return EmbedResult(dense_vector=self._text_to_vector(text))

    def embed_query(self, text: str) -> EmbedResult:
        return self.embed(text)

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


class SimpleInMemoryStorage:
    """Minimal in-memory storage for skill engine tests."""

    def __init__(self):
        self._collections: Dict[str, Dict[str, Dict[str, Any]]] = {}

    async def create_collection(self, name, schema) -> bool:
        if name not in self._collections:
            self._collections[name] = {}
            return True
        return False

    async def upsert(self, collection: str, data: Dict[str, Any]) -> str:
        if collection not in self._collections:
            self._collections[collection] = {}
        rid = data.get("id", str(uuid4()))
        data["id"] = rid
        self._collections[collection][rid] = dict(data)
        return rid

    async def get(self, collection: str, ids: List[str]) -> List[Dict[str, Any]]:
        col = self._collections.get(collection, {})
        return [dict(col[rid]) for rid in ids if rid in col]

    async def update(self, collection: str, rid: str, data: Dict[str, Any]) -> bool:
        col = self._collections.get(collection, {})
        if rid not in col:
            return False
        col[rid].update(data)
        return True

    async def delete(self, collection: str, ids: List[str]) -> int:
        col = self._collections.get(collection, {})
        count = 0
        for rid in ids:
            if rid in col:
                del col[rid]
                count += 1
        return count

    async def search(self, collection: str, query_vector=None,
                     sparse_query_vector=None, filter=None,
                     limit=10, **kwargs) -> List[Dict[str, Any]]:
        """Simple search: return all matching records (filter ignored in mock)."""
        col = self._collections.get(collection, {})
        results = list(col.values())
        # Simple status filter
        if filter and isinstance(filter, dict):
            status_conds = self._extract_status(filter)
            if status_conds:
                results = [r for r in results if r.get("status") in status_conds]
            vis_match = self._extract_visibility_match(filter)
            if vis_match:
                results = [r for r in results
                           if r.get("visibility") in vis_match
                           or r.get("user_id") == vis_match.get("user_id", "")]
        return results[:limit]

    async def filter(self, collection: str, filter: Dict, limit=10, **kwargs) -> List[Dict[str, Any]]:
        col = self._collections.get(collection, {})
        results = list(col.values())
        # Simple fingerprint filter
        if filter and filter.get("op") == "must" and filter.get("field") == "source_fingerprint":
            fp = filter["conds"][0]
            results = [r for r in results if r.get("source_fingerprint") == fp]
        return results[:limit]

    def _extract_status(self, f: Dict) -> Optional[List[str]]:
        """Extract status filter values from nested DSL."""
        if f.get("field") == "status":
            return f.get("conds", [])
        for child in f.get("conds", []):
            if isinstance(child, dict):
                result = self._extract_status(child)
                if result:
                    return result
        return None

    def _extract_visibility_match(self, f: Dict) -> Optional[Dict]:
        return None  # Simplified — visibility handled by SkillManager._is_visible

    def _all_records(self, collection: str) -> List[Dict[str, Any]]:
        """Test helper: return all records in a collection."""
        return list(self._collections.get(collection, {}).values())


async def mock_llm_completion(prompt: str) -> str:
    """Mock LLM that returns valid skill extraction JSON."""
    if "analyzing a cluster" in prompt.lower() or "memory cluster" in prompt.lower():
        return """[
            {
                "name": "deploy-staging-flow",
                "description": "Standard deployment to staging environment",
                "category": "workflow",
                "confidence": 0.9,
                "content": "# Deploy to Staging\\n\\n1. Run build\\n2. Run tests\\n3. Deploy to staging\\n4. Verify",
                "source_memory_ids": ["m1", "m2", "m3"]
            }
        ]"""
    if "EVOLUTION_COMPLETE" in prompt or "EVOLUTION_FAILED" in prompt:
        return "# Fixed Flow\n\n1. Better step\n\n<EVOLUTION_COMPLETE>"
    # Default: return a valid skill
    return "# Generated Skill\n\n1. Step one\n2. Step two\n\n<EVOLUTION_COMPLETE>"


# =============================================================================
# E2E Tests
# =============================================================================

class TestSkillEngineE2E(unittest.IsolatedAsyncioTestCase):
    """End-to-end Skill Engine tests with in-memory backends."""

    async def asyncSetUp(self):
        self.storage = SimpleInMemoryStorage()
        self.embedder = MockEmbedder()
        self.llm_adapter = LLMCompletionAdapter(mock_llm_completion)

        # Initialize skills collection
        self.skill_storage = SkillStorageAdapter(
            storage=self.storage,
            embedder=self.embedder,
            collection_name="skills",
            embedding_dim=4,
        )
        await self.skill_storage.initialize()

        self.store = SkillStore(self.skill_storage)
        self.evolver = SkillEvolver(llm=self.llm_adapter, store=self.store)

        # Source adapter reads from "context" collection
        await self.storage.create_collection("context", {})
        self.source = QdrantSourceAdapter(
            storage=self.storage,
            embedder=self.embedder,
            collection_name="context",
        )
        self.analyzer = SkillAnalyzer(
            source=self.source,
            llm=self.llm_adapter,
            store=self.store,
        )
        self.manager = SkillManager(
            store=self.store,
            analyzer=self.analyzer,
            evolver=self.evolver,
        )

        # Set request identity
        self._identity_tokens = set_request_identity("team1", "hugo")

    async def asyncTearDown(self):
        reset_request_identity(self._identity_tokens)

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    async def _seed_memories(self, count=5, topic="deployment"):
        """Seed memory collection with similar memories for clustering."""
        for i in range(count):
            await self.storage.upsert("context", {
                "id": f"m{i+1}",
                "uri": f"opencortex://team1/hugo/memories/events/m{i+1}",
                "abstract": f"{topic} step {i+1}: build and test and deploy",
                "overview": f"Overview of {topic} step {i+1}",
                "content": f"Detailed content about {topic} step {i+1}",
                "context_type": "memory",
                "category": "events",
                "scope": "private",
                "source_tenant_id": "team1",
                "source_user_id": "hugo",
                "project_id": "myproject",
                "is_leaf": True,
                "reward_score": 0.5,
                "vector": self.embedder.embed(f"{topic} step {i+1}").dense_vector,
            })

    def _make_skill(self, skill_id="sk-test", name="test-skill",
                    status=SkillStatus.ACTIVE,
                    visibility=SkillVisibility.PRIVATE,
                    user_id="hugo") -> SkillRecord:
        return SkillRecord(
            skill_id=skill_id, name=name,
            description="Test skill", content="# Test\n1. Step",
            category=SkillCategory.WORKFLOW,
            status=status, visibility=visibility,
            tenant_id="team1", user_id=user_id,
            uri=make_skill_uri("team1", user_id, skill_id),
            abstract="Test skill abstract",
        )

    # -----------------------------------------------------------------
    # 1. CRUD Lifecycle
    # -----------------------------------------------------------------

    async def test_01_save_and_load(self):
        """Save a skill and load it back."""
        skill = self._make_skill()
        await self.store.save_record(skill)

        loaded = await self.store.load_record("sk-test")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.skill_id, "sk-test")
        self.assertEqual(loaded.name, "test-skill")
        self.assertEqual(loaded.status, SkillStatus.ACTIVE)

    async def test_02_activate_and_deprecate(self):
        """Status transitions: CANDIDATE → ACTIVE → DEPRECATED."""
        skill = self._make_skill(status=SkillStatus.CANDIDATE)
        await self.store.save_record(skill)

        await self.store.activate("sk-test")
        loaded = await self.store.load_record("sk-test")
        self.assertEqual(loaded.status, SkillStatus.ACTIVE)

        await self.store.deprecate("sk-test")
        loaded = await self.store.load_record("sk-test")
        self.assertEqual(loaded.status, SkillStatus.DEPRECATED)

    async def test_03_approve_evolution(self):
        """approve_evolution: activate new, deprecate parent."""
        parent = self._make_skill(skill_id="sk-parent", status=SkillStatus.ACTIVE)
        child = self._make_skill(skill_id="sk-child", status=SkillStatus.CANDIDATE)
        child.lineage = SkillLineage(
            origin=SkillOrigin.FIXED,
            parent_skill_ids=["sk-parent"],
        )
        await self.store.save_record(parent)
        await self.store.save_record(child)

        await self.store.approve_evolution("sk-child", parent_ids=["sk-parent"])

        parent_loaded = await self.store.load_record("sk-parent")
        child_loaded = await self.store.load_record("sk-child")
        self.assertEqual(parent_loaded.status, SkillStatus.DEPRECATED)
        self.assertEqual(child_loaded.status, SkillStatus.ACTIVE)

    # -----------------------------------------------------------------
    # 2. Visibility & Authorization
    # -----------------------------------------------------------------

    async def test_04_private_skill_invisible_to_other_user(self):
        """PRIVATE skills not visible to other users."""
        skill = self._make_skill(user_id="alice", visibility=SkillVisibility.PRIVATE)
        await self.store.save_record(skill)

        # Hugo cannot see Alice's private skill
        result = await self.manager.get_skill("sk-test", "team1", "hugo")
        self.assertIsNone(result)

    async def test_05_shared_skill_visible_to_tenant(self):
        """SHARED skills visible to any user in tenant."""
        skill = self._make_skill(user_id="alice", visibility=SkillVisibility.SHARED)
        await self.store.save_record(skill)

        # Hugo can see Alice's shared skill
        result = await self.manager.get_skill("sk-test", "team1", "hugo")
        self.assertIsNotNone(result)

    async def test_06_non_owner_cannot_approve(self):
        """Non-owner cannot approve even a visible shared skill."""
        skill = self._make_skill(
            user_id="alice", visibility=SkillVisibility.SHARED,
            status=SkillStatus.CANDIDATE,
        )
        await self.store.save_record(skill)

        with self.assertRaises(ValueError) as ctx:
            await self.manager.approve("sk-test", "team1", "hugo")
        self.assertIn("owner", str(ctx.exception).lower())

    async def test_07_owner_can_approve(self):
        """Owner can approve their own skill."""
        skill = self._make_skill(status=SkillStatus.CANDIDATE)
        await self.store.save_record(skill)

        await self.manager.approve("sk-test", "team1", "hugo")
        loaded = await self.store.load_record("sk-test")
        self.assertEqual(loaded.status, SkillStatus.ACTIVE)

    async def test_08_cross_tenant_invisible(self):
        """Skills from different tenant are invisible."""
        skill = self._make_skill()
        skill.tenant_id = "other-team"
        await self.store.save_record(skill)

        result = await self.manager.get_skill("sk-test", "team1", "hugo")
        self.assertIsNone(result)

    # -----------------------------------------------------------------
    # 3. Promote (PRIVATE → SHARED)
    # -----------------------------------------------------------------

    async def test_09_promote_changes_visibility(self):
        """promote() changes PRIVATE to SHARED."""
        skill = self._make_skill(visibility=SkillVisibility.PRIVATE)
        await self.store.save_record(skill)

        await self.manager.promote("sk-test", "team1", "hugo")
        loaded = await self.store.load_record("sk-test")
        self.assertEqual(loaded.visibility, SkillVisibility.SHARED)

    async def test_10_promote_rejects_non_owner(self):
        """Non-owner cannot promote."""
        skill = self._make_skill(
            user_id="alice", visibility=SkillVisibility.SHARED,
        )
        await self.store.save_record(skill)

        with self.assertRaises(ValueError):
            await self.manager.promote("sk-test", "team1", "hugo")

    # -----------------------------------------------------------------
    # 4. Evolution
    # -----------------------------------------------------------------

    async def test_11_captured_evolution(self):
        """CAPTURED evolution creates a new skill from LLM output."""
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.CAPTURED,
            category=SkillCategory.WORKFLOW,
            direction="deploy-flow",
            source_memory_ids=["m1", "m2"],
        )
        result = await self.evolver.evolve(suggestion, "team1", "hugo")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, SkillStatus.CANDIDATE)
        self.assertEqual(result.lineage.origin, SkillOrigin.CAPTURED)
        self.assertEqual(result.visibility, SkillVisibility.PRIVATE)

    async def test_12_fix_evolution_creates_private_candidate(self):
        """FIX creates PRIVATE candidate regardless of parent visibility."""
        parent = self._make_skill(
            skill_id="sk-parent", status=SkillStatus.ACTIVE,
            visibility=SkillVisibility.SHARED,
        )
        await self.store.save_record(parent)

        result = await self.manager.fix_skill(
            "sk-parent", "team1", "hugo", "Fix outdated step",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, SkillStatus.CANDIDATE)
        self.assertEqual(result.visibility, SkillVisibility.PRIVATE)
        self.assertIn("sk-parent", result.lineage.parent_skill_ids)
        self.assertNotEqual(result.skill_id, "sk-parent")

        # Parent still ACTIVE
        parent_loaded = await self.store.load_record("sk-parent")
        self.assertEqual(parent_loaded.status, SkillStatus.ACTIVE)

    async def test_13_deterministic_skill_id_for_captured(self):
        """CAPTURED skills have deterministic ID from fingerprint — upsert idempotent."""
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.CAPTURED,
            category=SkillCategory.WORKFLOW,
            direction="deploy-flow",
            source_memory_ids=["m1", "m2", "m3"],
        )
        r1 = await self.evolver.evolve(suggestion, "team1", "hugo")
        r2 = await self.evolver.evolve(suggestion, "team1", "hugo")

        # Same source_memory_ids → same skill_id
        self.assertEqual(r1.skill_id, r2.skill_id)
        fp = make_source_fingerprint(["m1", "m2", "m3"])
        self.assertEqual(r1.skill_id, f"sk-{fp}")

    # -----------------------------------------------------------------
    # 5. Extraction Pipeline
    # -----------------------------------------------------------------

    async def test_14_extraction_available(self):
        """Manager reports extraction as available."""
        self.assertTrue(self.manager.extraction_available)

    async def test_15_extraction_unavailable_without_analyzer(self):
        """Manager without analyzer reports extraction unavailable."""
        mgr = SkillManager(store=self.store)
        self.assertFalse(mgr.extraction_available)
        with self.assertRaises(RuntimeError):
            await mgr.extract("team1", "hugo")

    async def test_16_full_extract_pipeline(self):
        """Full pipeline: seed memories → extract → candidates saved."""
        await self._seed_memories(count=5)

        results = await self.manager.extract("team1", "hugo")
        # Should extract at least 1 skill (LLM mock returns 1 per cluster)
        self.assertGreaterEqual(len(results), 1)
        for r in results:
            self.assertEqual(r.status, SkillStatus.CANDIDATE)
            self.assertEqual(r.tenant_id, "team1")
            self.assertEqual(r.user_id, "hugo")
            self.assertTrue(r.source_fingerprint)

    async def test_17_extract_idempotent(self):
        """Calling extract twice on same memories doesn't create duplicates."""
        await self._seed_memories(count=5)

        r1 = await self.manager.extract("team1", "hugo")
        r2 = await self.manager.extract("team1", "hugo")

        # Second call should return empty (fingerprint dedup)
        # or same skills (upsert idempotent)
        if r2:
            # If returned, should be same skill_ids (deterministic)
            ids1 = {r.skill_id for r in r1}
            ids2 = {r.skill_id for r in r2}
            self.assertEqual(ids1, ids2)

    # -----------------------------------------------------------------
    # 6. Ranker
    # -----------------------------------------------------------------

    async def test_18_ranker_prefers_matching_terms(self):
        """Ranker re-ranks by BM25 + embedding, preferring matching terms."""
        ranker = SkillRanker()
        deploy = SkillRecord(
            skill_id="sk-1", name="deploy-flow",
            description="Standard deployment workflow",
            content="# Deploy\n1. Build\n2. Test\n3. Deploy to staging",
            category=SkillCategory.WORKFLOW,
            tenant_id="t", user_id="u", abstract="deploy workflow",
        )
        debug = SkillRecord(
            skill_id="sk-2", name="debug-network",
            description="Debugging network issues",
            content="# Debug\n1. Check logs\n2. Trace packets",
            category=SkillCategory.PATTERN,
            tenant_id="t", user_id="u", abstract="debug network",
        )

        ranked = await ranker.rank("deploy staging", [debug, deploy])
        self.assertEqual(ranked[0].skill_id, "sk-1")

    # -----------------------------------------------------------------
    # 7. Full Lifecycle: Extract → Approve → Search
    # -----------------------------------------------------------------

    async def test_19_full_lifecycle(self):
        """Complete lifecycle: extract → approve → search finds it."""
        await self._seed_memories(count=5)

        # Extract
        candidates = await self.manager.extract("team1", "hugo")
        self.assertGreaterEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.status, SkillStatus.CANDIDATE)

        # Approve
        await self.manager.approve(candidate.skill_id, "team1", "hugo")
        loaded = await self.store.load_record(candidate.skill_id)
        self.assertEqual(loaded.status, SkillStatus.ACTIVE)

        # Search should find it
        results = await self.manager.search("deploy", "team1", "hugo")
        found_ids = {r.skill_id for r in results}
        self.assertIn(candidate.skill_id, found_ids)


if __name__ == "__main__":
    unittest.main()
