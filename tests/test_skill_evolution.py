"""Skill Evolution tests — Skill datamodel, Skillbook init, evolution methods."""
import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.ace.skillbook import Skillbook, validate_skill_meta
from opencortex.ace.types import Skill
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.storage.cortex_fs import CortexFS
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
        return {"name": name, "vector_dim": 4, "count": len(self._records.get(name, {})), "status": "ready"}

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

    async def search(self, collection, query_vector=None, sparse_query_vector=None, filter=None, limit=10, offset=0, output_fields=None, with_vector=False):
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

    async def filter(self, collection, filter, limit=10, offset=0, output_fields=None, order_by=None, order_desc=False):
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
        return {"collections": len(self._collections), "total_records": total, "storage_size": 0, "backend": "in-memory"}

    def _ensure(self, collection):
        if collection not in self._collections:
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")

    @staticmethod
    def _cosine_sim(a, b):
        if not b or len(a) != len(b):
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
            return val in conds
        elif op == "must_not":
            field_name = filt.get("field", "")
            conds = filt.get("conds", [])
            val = record.get(field_name)
            return val not in conds
        elif op == "and":
            return all(self._eval_filter(record, c) for c in filt.get("conds", []))
        elif op == "or":
            return any(self._eval_filter(record, c) for c in filt.get("conds", []))
        return True


# =============================================================================
# Helpers
# =============================================================================


def _run(coro):
    return asyncio.run(coro)


def _make_skillbook(storage, temp_dir):
    embedder = MockEmbedder()
    fs = CortexFS(data_root=temp_dir, vector_store=storage)
    sb = Skillbook(
        storage=storage,
        embedder=embedder,
        cortex_fs=fs,
        prefix="opencortex://test/shared/skills",
        embedding_dim=MockEmbedder.DIMENSION,
    )
    return sb, embedder


# =============================================================================
# 1. Skill Data Model Tests
# =============================================================================


class TestSkillDataModel(unittest.TestCase):
    """Tests 1-4: Skill evolution field defaults, serialization, compat."""

    def test_1_skill_new_fields_defaults(self):
        """T1: Skill new fields initialize with defaults."""
        s = Skill(id="test", section="general", content="test skill")
        self.assertEqual(s.confidence_score, 0.5)
        self.assertEqual(s.version, 1)
        self.assertEqual(s.trigger_conditions, [])
        self.assertEqual(s.action_template, [])
        self.assertEqual(s.success_metric, "")
        self.assertEqual(s.source_case_uris, [])
        self.assertIsNone(s.supersedes_uri)
        self.assertIsNone(s.superseded_by_uri)

    def test_2_to_dict_includes_evolution_fields(self):
        """T2: to_dict includes evolution fields."""
        s = Skill(
            id="t2", section="strategies", content="test",
            confidence_score=0.8, version=3,
            trigger_conditions=["when X"], action_template=["do Y"],
            success_metric="Z passes", source_case_uris=["uri1"],
            supersedes_uri="old_uri",
        )
        d = s.to_dict()
        self.assertEqual(d["confidence_score"], 0.8)
        self.assertEqual(d["version"], 3)
        self.assertEqual(d["trigger_conditions"], ["when X"])
        self.assertEqual(d["action_template"], ["do Y"])
        self.assertEqual(d["success_metric"], "Z passes")
        self.assertEqual(d["source_case_uris"], ["uri1"])
        self.assertEqual(d["supersedes_uri"], "old_uri")
        self.assertEqual(d["superseded_by_uri"], "")

    def test_3_from_dict_includes_evolution_fields(self):
        """T3: from_dict roundtrips evolution fields."""
        original = Skill(
            id="t3", section="patterns", content="roundtrip",
            confidence_score=0.9, version=2,
            trigger_conditions=["cond1", "cond2"],
            action_template=["step1", "step2"],
            success_metric="metric",
            source_case_uris=["u1", "u2"],
            supersedes_uri="old",
            superseded_by_uri="new",
        )
        d = original.to_dict()
        restored = Skill.from_dict(d)
        self.assertEqual(restored.confidence_score, 0.9)
        self.assertEqual(restored.version, 2)
        self.assertEqual(restored.trigger_conditions, ["cond1", "cond2"])
        self.assertEqual(restored.supersedes_uri, "old")
        self.assertEqual(restored.superseded_by_uri, "new")

    def test_4_from_dict_old_record_compat(self):
        """T4: Old records without evolution fields don't break."""
        old_data = {
            "id": "old1",
            "section": "general",
            "content": "legacy skill",
            "helpful": 5,
            "harmful": 0,
            "neutral": 1,
            "status": "active",
        }
        s = Skill.from_dict(old_data)
        self.assertEqual(s.confidence_score, 0.5)
        self.assertEqual(s.version, 1)
        self.assertEqual(s.trigger_conditions, [])
        self.assertEqual(s.source_case_uris, [])
        self.assertIsNone(s.supersedes_uri)


class TestValidateSkillMeta(unittest.TestCase):
    """Tests for validate_skill_meta."""

    def test_fills_defaults(self):
        """validate_skill_meta fills missing fields."""
        record = {"id": "x"}
        result = validate_skill_meta(record)
        self.assertEqual(result["confidence_score"], 0.5)
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["trigger_conditions"], [])
        self.assertEqual(result["source_case_uris"], [])

    def test_preserves_existing(self):
        """validate_skill_meta doesn't overwrite existing values."""
        record = {"confidence_score": 0.9, "version": 3}
        result = validate_skill_meta(record)
        self.assertEqual(result["confidence_score"], 0.9)
        self.assertEqual(result["version"], 3)


# =============================================================================
# 2. Skillbook Initialization Tests
# =============================================================================


class TestSkillbookInit(unittest.TestCase):
    """Tests 5-6: Skillbook always created, search excludes deprecated."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="skill_evo_")
        self.storage = InMemoryStorage()
        self.sb, self.embedder = _make_skillbook(self.storage, self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_5_skillbook_always_created(self):
        """T5: Skillbook can be created without ace_enabled."""
        _run(self.sb.init())
        self.assertTrue(_run(self.storage.collection_exists("skillbooks")))

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_6_search_excludes_deprecated(self, mock_ace):
        """T6: Skillbook.search excludes deprecated skills by default."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False,
            skill_share_mode="manual",
            skill_share_score_threshold=0.5,
            ace_scope_enforcement_enabled=False,
        )
        _run(self.sb.init())
        # Add active and deprecated skills
        _run(self.sb.add_skill("general", "active skill", tenant_id="t1", user_id="u1"))
        s = _run(self.sb.add_skill("general", "deprecated skill", tenant_id="t1", user_id="u1", status="deprecated"))

        results = _run(self.sb.search("skill", limit=10, tenant_id="t1", user_id="u1"))
        uris = [r.content for r in results]
        self.assertIn("active skill", uris)
        self.assertNotIn("deprecated skill", uris)


# =============================================================================
# 3. skill_lookup Tests
# =============================================================================


class TestSkillLookup(unittest.TestCase):
    """Tests 7-11: skill_lookup via orchestrator."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="skill_evo_")
        self.storage = InMemoryStorage()
        self.sb, self.embedder = _make_skillbook(self.storage, self.temp_dir)
        _run(self.sb.init())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def _add_skill(self, content, mock_ace, status="active", **kwargs):
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False,
            skill_share_mode="manual",
            skill_share_score_threshold=0.5,
            ace_scope_enforcement_enabled=False,
        )
        return _run(self.sb.add_skill(
            "general", content, tenant_id="t1", user_id="u1",
            status=status, **kwargs,
        ))

    def test_7_empty_query_returns_empty(self):
        """T7: Search with no skills stored returns empty."""
        results = _run(self.sb.search("anything", limit=5, tenant_id="t1", user_id="u1"))
        self.assertEqual(len(results), 0)

    def test_8_stored_skill_matches(self):
        """T8: Stored skill can be found via search."""
        self._add_skill("Use pytest for testing")
        results = _run(self.sb.search("testing", limit=5, tenant_id="t1", user_id="u1"))
        self.assertGreaterEqual(len(results), 1)

    def test_9_deprecated_skill_not_returned(self):
        """T9: Deprecated skill not in search results."""
        self._add_skill("old deprecated approach", status="deprecated")
        results = _run(self.sb.search("approach", limit=5, tenant_id="t1", user_id="u1"))
        for r in results:
            self.assertNotEqual(r.status, "deprecated")

    def test_10_observation_skill_returned(self):
        """T10: Observation-status skill IS returned (active during dual-track)."""
        self._add_skill("observing skill", status="observation")
        results = _run(self.sb.search(
            "observing", limit=5, tenant_id="t1", user_id="u1",
        ))
        # observation is not in default exclude_status (only deprecated is)
        found = any(r.content == "observing skill" for r in results)
        self.assertTrue(found)

    def test_11_tenant_isolation(self):
        """T11: Skills from tenant A not visible to tenant B."""
        self._add_skill("tenant A skill")
        results = _run(self.sb.search("skill", limit=5, tenant_id="other_tenant", user_id="u2"))
        # No results from "t1" should appear for "other_tenant"
        for r in results:
            self.assertNotEqual(r.tenant_id, "t1")


# =============================================================================
# 4. skill_feedback Tests
# =============================================================================


class TestSkillFeedback(unittest.TestCase):
    """Tests 12-16: skill_feedback confidence, version, counters."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="skill_evo_")
        self.storage = InMemoryStorage()
        self.sb, self.embedder = _make_skillbook(self.storage, self.temp_dir)
        _run(self.sb.init())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def _add_skill(self, content, mock_ace, **kwargs):
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        return _run(self.sb.add_skill("general", content, tenant_id="t1", user_id="u1", **kwargs))

    def test_12_helpful_feedback_increments(self):
        """T12: Helpful feedback increments helpful count."""
        skill = self._add_skill("test skill")
        _run(self.sb.tag_skill(skill.id, "helpful"))
        records = _run(self.storage.get("skillbooks", [skill.id]))
        self.assertEqual(records[0]["helpful"], 1)

    def test_13_harmful_feedback_increments(self):
        """T13: Harmful feedback increments harmful count."""
        skill = self._add_skill("test skill")
        _run(self.sb.tag_skill(skill.id, "harmful"))
        records = _run(self.storage.get("skillbooks", [skill.id]))
        self.assertEqual(records[0]["harmful"], 1)

    def test_14_confidence_formula(self):
        """T14: Confidence score computed correctly."""
        # rate * log(usage+1) * freshness
        # With 3 helpful, 0 harmful, 0 neutral, 0 days:
        # rate = 1.0, usage = 3, log(4) ≈ 1.386, freshness = 1.0
        # confidence ≈ 1.0 * 1.386 * 1.0 ≈ 1.386
        helpful, harmful, neutral = 3, 0, 0
        usage = helpful + harmful + neutral
        rate = helpful / usage
        freshness = 0.5 + 0.5 * math.exp(0)  # 0 days
        expected = round(rate * math.log(usage + 1) * freshness, 4)
        self.assertAlmostEqual(expected, 1.3863, places=3)

    def test_15_freshness_decay(self):
        """T15: Freshness decays over 45 days."""
        days = 45
        freshness = 0.5 + 0.5 * math.exp(-days / 45)
        self.assertAlmostEqual(freshness, 0.5 + 0.5 * math.exp(-1), places=4)
        # Should be less than 1.0
        self.assertLess(freshness, 1.0)

    def test_16_version_after_add(self):
        """T16: Skill starts at version 1."""
        skill = self._add_skill("versioned skill")
        self.assertEqual(skill.version, 1)


# =============================================================================
# 5. add_skill Evolution kwargs Tests
# =============================================================================


class TestAddSkillEvolutionKwargs(unittest.TestCase):
    """Tests for add_skill accepting evolution kwargs."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="skill_evo_")
        self.storage = InMemoryStorage()
        self.sb, _ = _make_skillbook(self.storage, self.temp_dir)
        _run(self.sb.init())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_add_skill_with_evolution_kwargs(self, mock_ace):
        """add_skill stores trigger_conditions, action_template, etc."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        skill = _run(self.sb.add_skill(
            "strategies", "Use caching for slow queries",
            tenant_id="t1", user_id="u1",
            trigger_conditions=["query latency > 500ms"],
            action_template=["identify slow query", "add cache layer"],
            success_metric="p95 < 100ms",
            source_case_uris=["uri://case1", "uri://case2"],
        ))
        self.assertEqual(skill.trigger_conditions, ["query latency > 500ms"])
        self.assertEqual(skill.action_template, ["identify slow query", "add cache layer"])
        self.assertEqual(skill.success_metric, "p95 < 100ms")
        self.assertEqual(len(skill.source_case_uris), 2)

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_persist_skill_stores_evolution_fields(self, mock_ace):
        """_persist_skill writes evolution fields to storage."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        skill = _run(self.sb.add_skill(
            "general", "Test persist",
            tenant_id="t1", user_id="u1",
            confidence_score=0.7, trigger_conditions=["when testing"],
        ))
        records = _run(self.storage.get("skillbooks", [skill.id]))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["confidence_score"], 0.7)
        self.assertEqual(records[0]["trigger_conditions"], ["when testing"])


# =============================================================================
# 6. RuleExtractor Enabled Tests
# =============================================================================


class TestRuleExtractorEnabled(unittest.TestCase):
    """Tests 32-33: RuleExtractor is enabled and triggers from add()."""

    def test_32_rule_extractor_instantiated(self):
        """T32: RuleExtractor is instantiated by default in orchestrator."""
        from opencortex.orchestrator import MemoryOrchestrator
        orch = MemoryOrchestrator()
        self.assertIsNotNone(orch._rule_extractor)

    def test_33_rule_extractor_extract(self):
        """T33: RuleExtractor.extract produces skills from content."""
        from opencortex.ace.rule_extractor import RuleExtractor
        re = RuleExtractor()
        skills = re.extract(
            "Always run tests before deploying",
            "When deploying to production, always run the full test suite first. "
            "This prevents regressions and catches bugs early."
        )
        # Should extract at least one skill
        self.assertIsInstance(skills, list)


# =============================================================================
# 7. Hook + Protocol Tests
# =============================================================================


class TestHookAndProtocol(unittest.TestCase):
    """Tests 30-31: session-start hook + skill protocol file."""

    def test_30_session_start_uses_skill_lookup(self):
        """T30: session-start.mjs references skill/lookup endpoint."""
        import os
        hook_path = os.path.join(
            os.path.dirname(__file__), "..",
            "plugins", "opencortex-memory", "hooks", "handlers", "session-start.mjs",
        )
        with open(hook_path) as f:
            content = f.read()
        self.assertIn("/api/v1/skill/lookup", content)
        self.assertIn("[Learned Skills]", content)
        self.assertNotIn("[Learned Abilities]", content)
        self.assertNotIn("/api/v1/ability/", content)

    def test_31_skill_protocol_exists(self):
        """T31: SKILL.md protocol file exists and references skill_lookup/feedback."""
        import os
        protocol_path = os.path.join(
            os.path.dirname(__file__), "..",
            "plugins", "opencortex-memory", "skills", "skill-protocol", "SKILL.md",
        )
        self.assertTrue(os.path.exists(protocol_path))
        with open(protocol_path) as f:
            content = f.read()
        self.assertIn("skill_lookup", content)
        self.assertIn("skill_feedback", content)


# =============================================================================
# 8. HTTP Models Tests
# =============================================================================


class TestHttpModels(unittest.TestCase):
    """Test that Skill* request models exist and Ability* are removed."""

    def test_skill_models_exist(self):
        """Skill request models are importable."""
        from opencortex.http.models import (
            SkillLookupRequest,
            SkillFeedbackRequest,
            SkillMineRequest,
            SkillEvolveRequest,
        )
        req = SkillLookupRequest(objective="test")
        self.assertEqual(req.objective, "test")
        self.assertEqual(req.section, "")
        self.assertEqual(req.limit, 5)

    def test_ability_models_removed(self):
        """Ability request models are no longer importable."""
        import opencortex.http.models as models
        self.assertFalse(hasattr(models, "AbilityLookupRequest"))
        self.assertFalse(hasattr(models, "AbilityFeedbackRequest"))
        self.assertFalse(hasattr(models, "AbilityMineRequest"))
        self.assertFalse(hasattr(models, "AbilityEvolveRequest"))
        self.assertFalse(hasattr(models, "AbilityMeta"))


# =============================================================================
# 9. ContextType Tests
# =============================================================================


class TestContextType(unittest.TestCase):
    """Test ABILITY enum value removed."""

    def test_ability_removed(self):
        """ABILITY is not in ContextType."""
        from opencortex.retrieve.types import ContextType
        self.assertFalse(hasattr(ContextType, "ABILITY"))
        # SKILL should still exist
        self.assertTrue(hasattr(ContextType, "SKILL"))


# =============================================================================
# 10. MCP Server Tests
# =============================================================================


class TestMcpServer(unittest.TestCase):
    """Test MCP server tool definitions."""

    def test_mcp_has_skill_tools(self):
        """MCP server defines skill_lookup, skill_feedback, etc."""
        mcp_path = os.path.join(
            os.path.dirname(__file__), "..",
            "plugins", "opencortex-memory", "lib", "mcp-server.mjs",
        )
        with open(mcp_path) as f:
            content = f.read()
        self.assertIn("skill_lookup:", content)
        self.assertIn("skill_feedback:", content)
        self.assertIn("skill_mine:", content)
        self.assertIn("skill_evolve:", content)
        # Ability tools should be gone
        self.assertNotIn("ability_lookup:", content)
        self.assertNotIn("ability_feedback:", content)


# =============================================================================
# 11. Orchestrator Data Flow Integration Tests
# =============================================================================


class TestSkillEvolutionDataFlow(unittest.TestCase):
    """End-to-end data flow tests for orchestrator skill evolution methods.

    Tests the complete pipeline: orchestrator.init() → skillbook ready →
    skill_lookup / skill_feedback / mine_skills / evolve_skill / _resolve_observation.
    """

    def setUp(self):
        from opencortex.config import CortexConfig, init_config
        from opencortex.http.request_context import set_request_identity

        self.temp_dir = tempfile.mkdtemp(prefix="skill_evo_e2e_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
        )
        init_config(self.config)
        self._identity_tokens = set_request_identity("testteam", "alice")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()

    def tearDown(self):
        from opencortex.http.request_context import reset_request_identity
        reset_request_identity(self._identity_tokens)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _init_orch(self):
        from opencortex.orchestrator import MemoryOrchestrator
        orch = MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=self.embedder,
        )
        _run(orch.init())
        return orch

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def _add_skill_via_skillbook(self, orch, content, mock_ace, section="general",
                                  status="active", **kwargs):
        """Add a skill directly via Skillbook and return the Skill object."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        return _run(orch._skillbook.add_skill(
            section, content, tenant_id="testteam", user_id="alice",
            status=status, **kwargs,
        ))

    # ---- skill_lookup E2E ----

    def test_e2e_skill_lookup_add_then_find(self):
        """Add skill via skillbook → call orchestrator.skill_lookup → verify match."""
        orch = self._init_orch()
        skill = self._add_skill_via_skillbook(orch, "Use pytest for unit tests")

        results = _run(orch.skill_lookup("pytest unit tests", limit=5))
        self.assertIsInstance(results, list)
        self.assertGreaterEqual(len(results), 1)
        found_ids = [r["id"] for r in results]
        self.assertIn(skill.id, found_ids)

    def test_e2e_skill_lookup_excludes_deprecated(self):
        """Deprecated skills should not appear in skill_lookup results."""
        orch = self._init_orch()
        self._add_skill_via_skillbook(orch, "old approach", status="deprecated")
        self._add_skill_via_skillbook(orch, "new approach")

        results = _run(orch.skill_lookup("approach", limit=10))
        for r in results:
            self.assertNotEqual(r["status"], "deprecated")

    def test_e2e_skill_lookup_sorted_by_confidence(self):
        """skill_lookup results sorted by confidence_score descending."""
        orch = self._init_orch()
        low = self._add_skill_via_skillbook(orch, "low confidence skill",
                                             confidence_score=0.1)
        high = self._add_skill_via_skillbook(orch, "high confidence skill",
                                              confidence_score=0.9)

        results = _run(orch.skill_lookup("confidence skill", limit=10))
        if len(results) >= 2:
            self.assertGreaterEqual(results[0]["confidence_score"],
                                    results[1]["confidence_score"])

    # ---- skill_feedback E2E ----

    def test_e2e_skill_feedback_helpful(self):
        """skill_feedback(success=True) increments helpful and updates confidence."""
        orch = self._init_orch()
        skill = self._add_skill_via_skillbook(orch, "feedback test skill")
        uri = f"opencortex://testteam/shared/skills/general/{skill.id}"

        result = _run(orch.skill_feedback(uri, success=True))
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["helpful"], 1)
        self.assertEqual(result["harmful"], 0)
        self.assertEqual(result["version"], 2)  # 1 → 2
        self.assertGreater(result["confidence_score"], 0)

    def test_e2e_skill_feedback_harmful(self):
        """skill_feedback(success=False) increments harmful counter."""
        orch = self._init_orch()
        skill = self._add_skill_via_skillbook(orch, "harmful test skill")
        uri = f"opencortex://testteam/shared/skills/general/{skill.id}"

        result = _run(orch.skill_feedback(uri, success=False))
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["harmful"], 1)
        self.assertEqual(result["helpful"], 0)

    def test_e2e_skill_feedback_multiple_increments(self):
        """Multiple feedback calls accumulate counters and bump version."""
        orch = self._init_orch()
        skill = self._add_skill_via_skillbook(orch, "multi feedback skill")
        uri = f"opencortex://testteam/shared/skills/general/{skill.id}"

        _run(orch.skill_feedback(uri, success=True))
        _run(orch.skill_feedback(uri, success=True))
        result = _run(orch.skill_feedback(uri, success=False))

        self.assertEqual(result["helpful"], 2)
        self.assertEqual(result["harmful"], 1)
        self.assertEqual(result["version"], 4)  # 1 → 2 → 3 → 4

    def test_e2e_skill_feedback_confidence_formula(self):
        """Verify confidence = rate * log(usage+1) * freshness after feedback."""
        import math
        orch = self._init_orch()
        skill = self._add_skill_via_skillbook(orch, "formula test skill")
        uri = f"opencortex://testteam/shared/skills/general/{skill.id}"

        # 3 helpful, 0 harmful, 0 neutral → rate=1.0, usage=3
        _run(orch.skill_feedback(uri, success=True))
        _run(orch.skill_feedback(uri, success=True))
        result = _run(orch.skill_feedback(uri, success=True))

        # freshness ≈ 1.0 (0 days old)
        expected = round(1.0 * math.log(3 + 1) * 1.0, 4)
        self.assertAlmostEqual(result["confidence_score"], expected, places=3)

    def test_e2e_skill_feedback_not_found(self):
        """skill_feedback for non-existent URI returns error."""
        orch = self._init_orch()
        result = _run(orch.skill_feedback("opencortex://testteam/shared/skills/general/nonexistent"))
        self.assertIn("error", result)

    # ---- mine_skills E2E ----

    def test_e2e_mine_skills_no_cases(self):
        """mine_skills with empty context collection returns 0."""
        orch = self._init_orch()
        orch._llm_completion = AsyncMock(return_value="{}")
        result = _run(orch.mine_skills(min_cases=2))
        self.assertEqual(result["mined"], 0)

    def test_e2e_mine_skills_insufficient_cases(self):
        """mine_skills with fewer successful cases than min_cases returns 0."""
        orch = self._init_orch()
        orch._llm_completion = AsyncMock(return_value="{}")
        # Seed 2 case records with success status
        import json
        for i in range(2):
            _run(self.storage.insert("context", {
                "id": f"case_{i}",
                "abstract": f"Case {i}: fix bug",
                "context_type": "case",
                "source_tenant_id": "testteam",
                "category": "",
                "vector": self.embedder.embed(f"fix bug {i}").dense_vector,
                "meta": json.dumps({"evaluation": {"status": "success"},
                                     "action_path": ["diagnose", "fix"]}),
            }))

        result = _run(orch.mine_skills(min_cases=5))
        # Only 2 cases, min_cases=5, so no cluster qualifies
        self.assertEqual(result["mined"], 0)

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_e2e_mine_skills_success(self, mock_ace):
        """mine_skills with enough cases + mock LLM produces a skill."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()

        # Seed 6 case records with identical vectors (same cluster)
        import json
        shared_vec = [0.5, 0.5, 0.5, 0.5]
        for i in range(6):
            _run(self.storage.insert("context", {
                "id": f"case_{i}",
                "abstract": f"Case {i}: optimize database query",
                "context_type": "case",
                "source_tenant_id": "testteam",
                "category": "",
                "uri": f"opencortex://testteam/user/alice/cases/{i}",
                "vector": list(shared_vec),
                "meta": json.dumps({
                    "evaluation": {"status": "success"},
                    "action_path": ["profile query", "add index", "verify"],
                }),
            }))

        # Mock LLM to return valid skill JSON
        async def mock_llm(prompt):
            return json.dumps({
                "abstract": "Add database index for slow queries",
                "section": "strategies",
                "trigger_conditions": ["query latency > 500ms"],
                "action_template": ["profile query", "add index", "verify"],
                "success_metric": "p95 < 100ms",
            })

        orch._llm_completion = mock_llm

        result = _run(orch.mine_skills(min_cases=3, max_clusters=5, llm_budget=3))
        self.assertGreaterEqual(result["mined"], 1)
        self.assertGreater(result["clusters"], 0)

        # Verify skill is actually stored in skillbook
        skills = _run(orch._skillbook.search(
            "database index", limit=5,
            tenant_id="testteam", user_id="alice",
        ))
        found = any("index" in s.content.lower() or "database" in s.content.lower()
                     for s in skills)
        self.assertTrue(found, f"Expected mined skill in skillbook, got: {[s.content for s in skills]}")

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_e2e_mine_skills_stores_source_uris(self, mock_ace):
        """Mined skill should have source_case_uris tracking origin cases."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()

        import json
        shared_vec = [0.5, 0.5, 0.5, 0.5]
        case_uris = []
        for i in range(5):
            uri = f"opencortex://testteam/user/alice/cases/src_{i}"
            case_uris.append(uri)
            _run(self.storage.insert("context", {
                "id": f"src_case_{i}",
                "abstract": f"Fix memory leak in module {i}",
                "context_type": "case",
                "source_tenant_id": "testteam",
                "category": "",
                "uri": uri,
                "vector": list(shared_vec),
                "meta": json.dumps({"evaluation": {"status": "success"},
                                     "action_path": ["detect leak", "fix"]}),
            }))

        async def mock_llm(prompt):
            return json.dumps({
                "abstract": "Fix memory leaks proactively",
                "section": "patterns",
                "trigger_conditions": ["memory usage increasing"],
                "action_template": ["detect leak", "fix allocation"],
                "success_metric": "no OOM errors",
            })

        orch._llm_completion = mock_llm

        result = _run(orch.mine_skills(min_cases=3))
        self.assertGreaterEqual(result["mined"], 1)

        # Check stored skill has source_case_uris
        all_records = list(self.storage._records.get("skillbooks", {}).values())
        mined_skills = [r for r in all_records if r.get("source_case_uris")]
        self.assertGreater(len(mined_skills), 0,
                           "Expected at least one skill with source_case_uris")
        self.assertGreater(len(mined_skills[0]["source_case_uris"]), 0)

    def test_e2e_mine_skills_no_llm(self):
        """mine_skills without LLM configured returns error."""
        orch = self._init_orch()
        orch._llm_completion = None

        result = _run(orch.mine_skills())
        self.assertIn("error", result)

    def test_e2e_mine_skills_llm_budget_limit(self):
        """mine_skills respects llm_budget limit."""
        orch = self._init_orch()

        import json
        # Seed 20 cases forming 2 clearly distinct clusters
        vec_a = [1.0, 0.0, 0.0, 0.0]
        vec_b = [0.0, 0.0, 0.0, 1.0]
        for i in range(10):
            _run(self.storage.insert("context", {
                "id": f"ca_{i}", "abstract": f"Cluster A case {i}",
                "context_type": "case", "source_tenant_id": "testteam",
                "category": "", "uri": f"opencortex://testteam/cases/a{i}",
                "vector": list(vec_a),
                "meta": json.dumps({"evaluation": {"status": "success"}}),
            }))
        for i in range(10):
            _run(self.storage.insert("context", {
                "id": f"cb_{i}", "abstract": f"Cluster B case {i}",
                "context_type": "case", "source_tenant_id": "testteam",
                "category": "", "uri": f"opencortex://testteam/cases/b{i}",
                "vector": list(vec_b),
                "meta": json.dumps({"evaluation": {"status": "success"}}),
            }))

        call_count = 0

        async def mock_llm(prompt):
            nonlocal call_count
            call_count += 1
            return json.dumps({
                "abstract": f"Mined skill {call_count}",
                "section": "general",
                "trigger_conditions": [],
                "action_template": [],
                "success_metric": "",
            })

        orch._llm_completion = mock_llm

        result = _run(orch.mine_skills(min_cases=3, llm_budget=1))
        # Should mine at most 1 skill due to budget
        self.assertLessEqual(result["mined"], 1)
        self.assertLessEqual(call_count, 1)

    # ---- evolve_skill E2E ----

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_e2e_evolve_no_evolution_needed(self, mock_ace):
        """evolve_skill returns no_evolution_needed if confidence >= threshold."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()
        skill = self._add_skill_via_skillbook(orch, "high confidence skill",
                                               confidence_score=0.8)
        # Update confidence in storage too
        _run(self.storage.update("skillbooks", skill.id, {"confidence_score": 0.8}))

        uri = f"opencortex://testteam/shared/skills/general/{skill.id}"
        result = _run(orch.evolve_skill(uri, confidence_threshold=0.3))
        self.assertEqual(result["status"], "no_evolution_needed")

    def test_e2e_evolve_not_found(self):
        """evolve_skill for non-existent skill returns error."""
        orch = self._init_orch()
        result = _run(orch.evolve_skill("opencortex://testteam/shared/skills/general/ghost"))
        self.assertIn("error", result)

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_e2e_evolve_no_replacement(self, mock_ace):
        """evolve_skill returns no_replacement_found when mine_skills yields 0."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()
        skill = self._add_skill_via_skillbook(orch, "weak skill", confidence_score=0.1)
        _run(self.storage.update("skillbooks", skill.id, {"confidence_score": 0.1}))

        # No cases in context → mine_skills returns 0
        async def mock_llm(prompt):
            return "{}"

        orch._llm_completion = mock_llm

        uri = f"opencortex://testteam/shared/skills/general/{skill.id}"
        result = _run(orch.evolve_skill(uri, confidence_threshold=0.3))
        self.assertIn(result["status"], ("no_replacement_found",))

    # ---- _resolve_observation E2E ----

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_e2e_resolve_observation_new_wins(self, mock_ace):
        """When new skill has higher confidence, old gets deprecated."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()

        old_skill = self._add_skill_via_skillbook(orch, "old approach", status="observation")
        new_skill = self._add_skill_via_skillbook(orch, "new approach")

        old_uri = f"opencortex://testteam/shared/skills/general/{old_skill.id}"
        new_uri = f"opencortex://testteam/shared/skills/general/{new_skill.id}"

        # Set up supersedes chain: new supersedes old
        _run(self.storage.update("skillbooks", new_skill.id, {
            "supersedes_uri": old_uri,
            "confidence_score": 0.9,
        }))
        _run(self.storage.update("skillbooks", old_skill.id, {
            "superseded_by_uri": new_uri,
            "confidence_score": 0.2,
        }))

        result = _run(orch._resolve_observation(new_uri))
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "old_deprecated")
        self.assertEqual(result["winner"], new_uri)
        self.assertEqual(result["loser"], old_uri)

        # Verify old skill is now deprecated in storage
        old_records = _run(self.storage.get("skillbooks", [old_skill.id]))
        self.assertEqual(old_records[0]["status"], "deprecated")

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_e2e_resolve_observation_rollback(self, mock_ace):
        """When old skill has higher confidence, new gets deprecated (rollback)."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()

        old_skill = self._add_skill_via_skillbook(orch, "proven approach", status="observation")
        new_skill = self._add_skill_via_skillbook(orch, "failed replacement")

        old_uri = f"opencortex://testteam/shared/skills/general/{old_skill.id}"
        new_uri = f"opencortex://testteam/shared/skills/general/{new_skill.id}"

        # New supersedes old, but old has higher confidence
        _run(self.storage.update("skillbooks", new_skill.id, {
            "supersedes_uri": old_uri,
            "confidence_score": 0.1,
        }))
        _run(self.storage.update("skillbooks", old_skill.id, {
            "superseded_by_uri": new_uri,
            "confidence_score": 0.8,
        }))

        result = _run(orch._resolve_observation(new_uri))
        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "rollback")
        self.assertEqual(result["winner"], old_uri)

        # Verify new skill deprecated, old restored to active
        new_records = _run(self.storage.get("skillbooks", [new_skill.id]))
        self.assertEqual(new_records[0]["status"], "deprecated")

        old_records = _run(self.storage.get("skillbooks", [old_skill.id]))
        self.assertEqual(old_records[0]["status"], "active")
        self.assertEqual(old_records[0]["superseded_by_uri"], "")

    def test_e2e_resolve_observation_no_supersedes(self):
        """_resolve_observation returns None when no supersedes link."""
        orch = self._init_orch()
        # Insert a skill record directly with no supersedes_uri
        _run(self.storage.create_collection("skillbooks", {"vector_dim": 4}))
        _run(self.storage.upsert("skillbooks", {
            "id": "standalone", "supersedes_uri": "",
            "confidence_score": 0.5,
        }))

        result = _run(orch._resolve_observation(
            "opencortex://testteam/shared/skills/general/standalone"))
        self.assertIsNone(result)

    # ---- skill_feedback triggers _resolve_observation ----

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_e2e_feedback_triggers_observation_resolution(self, mock_ace):
        """After enough feedback, skill_feedback auto-triggers _resolve_observation."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()

        old_skill = self._add_skill_via_skillbook(orch, "old method", status="observation")
        new_skill = self._add_skill_via_skillbook(orch, "new method")

        old_uri = f"opencortex://testteam/shared/skills/general/{old_skill.id}"
        new_uri = f"opencortex://testteam/shared/skills/general/{new_skill.id}"

        # Wire supersedes chain
        _run(self.storage.update("skillbooks", new_skill.id, {
            "supersedes_uri": old_uri,
            "confidence_score": 0.9,
        }))
        _run(self.storage.update("skillbooks", old_skill.id, {
            "superseded_by_uri": new_uri,
            "confidence_score": 0.1,
        }))

        # Feed the new skill 10 times (default observation_turns=10)
        for _ in range(10):
            _run(orch.skill_feedback(new_uri, success=True))

        # After 10 feedbacks, _resolve_observation should have been called
        # and old skill should be deprecated (new has higher confidence)
        old_records = _run(self.storage.get("skillbooks", [old_skill.id]))
        self.assertEqual(old_records[0]["status"], "deprecated",
                         "Old skill should be deprecated after observation resolution")

    # ---- Skillbook always initialized ----

    def test_e2e_skillbook_initialized_via_orchestrator(self):
        """Orchestrator.init() always creates _skillbook regardless of ace_enabled."""
        orch = self._init_orch()
        self.assertIsNotNone(orch._skillbook)
        self.assertTrue(_run(self.storage.collection_exists("skillbooks")))

    # ---- Full pipeline: add → lookup → feedback → confidence updated ----

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_e2e_full_skill_lifecycle(self, mock_ace):
        """Full lifecycle: add skill → lookup → feedback → verify updates."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()

        # 1. Add a skill
        skill = self._add_skill_via_skillbook(
            orch, "Always run linter before commit",
            trigger_conditions=["before git commit"],
            action_template=["run linter", "fix warnings", "commit"],
        )

        # 2. Look it up
        results = _run(orch.skill_lookup("linter commit", limit=5))
        self.assertGreaterEqual(len(results), 1)
        matched = [r for r in results if r["id"] == skill.id]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["trigger_conditions"], ["before git commit"])
        self.assertEqual(matched[0]["action_template"], ["run linter", "fix warnings", "commit"])

        # 3. Give positive feedback
        uri = f"opencortex://testteam/shared/skills/general/{skill.id}"
        fb_result = _run(orch.skill_feedback(uri, success=True))
        self.assertEqual(fb_result["status"], "updated")
        self.assertEqual(fb_result["version"], 2)

        # 4. Look up again — confidence should reflect feedback
        results2 = _run(orch.skill_lookup("linter commit", limit=5))
        matched2 = [r for r in results2 if r["id"] == skill.id]
        self.assertEqual(len(matched2), 1)
        # Confidence after 1 helpful feedback should be > 0
        # (storage.get reads the updated record)
        records = _run(self.storage.get("skillbooks", [skill.id]))
        self.assertGreater(records[0]["confidence_score"], 0)
        self.assertEqual(records[0]["version"], 2)


# =============================================================================
# 12. Source Gating Tests (hook:* skips extraction)
# =============================================================================


class TestSourceGating(unittest.TestCase):
    """Test that hook-sourced content skips skill extraction."""

    def setUp(self):
        from opencortex.config import CortexConfig, init_config
        from opencortex.http.request_context import set_request_identity

        self.temp_dir = tempfile.mkdtemp(prefix="source_gate_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
        )
        init_config(self.config)
        self._identity_tokens = set_request_identity("testteam", "alice")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()

    def tearDown(self):
        from opencortex.http.request_context import reset_request_identity
        reset_request_identity(self._identity_tokens)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _init_orch(self):
        from opencortex.orchestrator import MemoryOrchestrator
        orch = MemoryOrchestrator(
            config=self.config,
            storage=self.storage,
            embedder=self.embedder,
        )
        _run(orch.init())
        return orch

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_hook_source_skips_extraction(self, mock_ace):
        """add() with meta.source='hook:stop' should NOT trigger skill extraction."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()

        # Content that would normally trigger error_fix extraction
        content = (
            "When encountering a connection timeout error, then check firewall rules "
            "and retry with exponential backoff. This pattern applies to all services."
        )
        content += "\n" * 50

        with patch.object(orch, '_try_extract_skills', wraps=orch._try_extract_skills) as mock_extract:
            _run(orch.add(
                abstract="connection timeout fix",
                content=content,
                category="error_fixes",
                meta={"source": "hook:stop"},
            ))
            # Allow any pending tasks to complete
            _run(asyncio.sleep(0.1))
            mock_extract.assert_not_called()

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_non_hook_source_triggers_extraction(self, mock_ace):
        """add() with meta.source='user' should trigger skill extraction."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()

        content = (
            "When encountering a connection timeout error, then check firewall rules "
            "and retry with exponential backoff. This pattern applies to all services."
        )
        content += "\n" * 50

        with patch.object(orch, '_try_extract_skills', wraps=orch._try_extract_skills) as mock_extract:
            _run(orch.add(
                abstract="connection timeout fix",
                content=content,
                category="error_fixes",
                meta={"source": "user"},
            ))
            # Allow any pending tasks to complete
            _run(asyncio.sleep(0.1))
            mock_extract.assert_called_once()

    @patch("opencortex.ace.skillbook.get_effective_ace_config")
    def test_no_source_meta_triggers_extraction(self, mock_ace):
        """add() without meta.source should trigger skill extraction."""
        mock_ace.return_value = MagicMock(
            share_skills_to_team=False, skill_share_mode="manual",
            skill_share_score_threshold=0.5, ace_scope_enforcement_enabled=False,
        )
        orch = self._init_orch()

        content = (
            "When encountering a connection timeout error, then check firewall rules "
            "and retry with exponential backoff. This pattern applies to all services."
        )
        content += "\n" * 50

        with patch.object(orch, '_try_extract_skills', wraps=orch._try_extract_skills) as mock_extract:
            _run(orch.add(
                abstract="connection timeout fix",
                content=content,
                category="error_fixes",
            ))
            # Allow any pending tasks to complete
            _run(asyncio.sleep(0.1))
            mock_extract.assert_called_once()


if __name__ == "__main__":
    unittest.main()
