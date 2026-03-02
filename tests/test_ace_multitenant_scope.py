"""
ACE Multi-Tenant Scope tests — sharing engine, query isolation, hard block, share score.

Uses in-memory mocks (no external binary or network calls needed).
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.ace.skillbook import Skillbook, SkillAuthorizationError
from opencortex.ace.types import Skill
from opencortex.config import CortexConfig
from opencortex.http.request_context import ACEConfig, set_request_ace_config, reset_request_ace_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.storage.cortex_fs import CortexFS
from opencortex.storage.vikingdb_interface import (
    CollectionNotFoundError,
    VikingDBInterface,
)


# =============================================================================
# Mocks (same as test_ace_phase1)
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


class InMemoryStorage(VikingDBInterface):
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
        return {"name": name} if name in self._collections else None

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
        return await self.delete(collection, [r["id"] for r in records])

    async def remove_by_uri(self, collection, uri):
        self._ensure(collection)
        to_remove = [rid for rid, r in self._records[collection].items() if r.get("uri", "").startswith(uri)]
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
        return {"collections": len(self._collections), "total_records": 0, "storage_size": 0, "backend": "in-memory"}

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
        return dot / (na * nb) if na and nb else 0.0

    def _eval_filter(self, record, filt):
        if not filt:
            return True
        op = filt.get("op", "")
        if op == "must":
            return record.get(filt.get("field", "")) in filt.get("conds", [])
        elif op == "and":
            return all(self._eval_filter(record, c) for c in filt.get("conds", []))
        elif op == "or":
            return any(self._eval_filter(record, c) for c in filt.get("conds", []))
        elif op == "prefix":
            return str(record.get(filt.get("field", ""), "")).startswith(filt.get("prefix", ""))
        elif op == "range":
            val = record.get(filt.get("field", ""), 0)
            if "gte" in filt and val < filt["gte"]:
                return False
            if "lte" in filt and val > filt["lte"]:
                return False
            return True
        return True


def _run(coro):
    return asyncio.run(coro)


def _make_config(**overrides) -> ACEConfig:
    """Create an ACEConfig with overrides for testing."""
    return ACEConfig(**overrides)


def _make_skillbook(storage, temp_dir):
    embedder = MockEmbedder()
    fs = CortexFS(data_root=temp_dir, vector_store=storage)
    sb = Skillbook(
        storage=storage,
        embedder=embedder,
        cortex_fs=fs,
        prefix="opencortex://default/user/default/skillbooks",
        embedding_dim=MockEmbedder.DIMENSION,
    )
    _run(sb.init())
    return sb


# =============================================================================
# Hard Block Check Tests
# =============================================================================


class TestHardBlockCheck(unittest.TestCase):
    """Test _hard_block_check catches sensitive content."""

    def test_01_secret_api_key(self):
        """Content with api_key= is blocked."""
        blocked, reason = Skillbook._hard_block_check("Use api_key=sk-12345 for auth")
        self.assertTrue(blocked)
        self.assertEqual(reason, "contains_secret")

    def test_02_secret_password(self):
        """Content with password: is blocked."""
        blocked, reason = Skillbook._hard_block_check("Set password: hunter2")
        self.assertTrue(blocked)
        self.assertEqual(reason, "contains_secret")

    def test_03_secret_token(self):
        """Content with token= is blocked."""
        blocked, reason = Skillbook._hard_block_check("Export TOKEN=abc123")
        self.assertTrue(blocked)
        self.assertEqual(reason, "contains_secret")

    def test_04_pii_email(self):
        """Content with email address is blocked."""
        blocked, reason = Skillbook._hard_block_check("Contact alice@example.com for help")
        self.assertTrue(blocked)
        self.assertEqual(reason, "contains_pii_email")

    def test_05_pii_phone(self):
        """Content with phone number is blocked."""
        blocked, reason = Skillbook._hard_block_check("Call 13912345678 for support")
        self.assertTrue(blocked)
        self.assertEqual(reason, "contains_pii_phone")

    def test_06_pii_idcard(self):
        """Content with ID card number is blocked."""
        blocked, reason = Skillbook._hard_block_check("ID: 110101199001011234")
        self.assertTrue(blocked)
        self.assertEqual(reason, "contains_pii_idcard")

    def test_07_env_path(self):
        """Content with absolute path is blocked."""
        blocked, reason = Skillbook._hard_block_check("Check /Users/alice/.ssh/config")
        self.assertTrue(blocked)
        self.assertEqual(reason, "contains_env_path")

    def test_08_internal_host(self):
        """Content with internal hostname is blocked."""
        blocked, reason = Skillbook._hard_block_check("Deploy to app.staging.internal")
        self.assertTrue(blocked)
        self.assertEqual(reason, "contains_internal_host")

    def test_09_clean_content_passes(self):
        """Clean content is not blocked."""
        blocked, reason = Skillbook._hard_block_check(
            "Run pytest before committing to verify all tests pass"
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")

    def test_10_generic_technical_passes(self):
        """Generic technical content is not blocked."""
        blocked, reason = Skillbook._hard_block_check(
            "Use dependency injection when creating service classes"
        )
        self.assertFalse(blocked)
        self.assertEqual(reason, "")


# =============================================================================
# Share Score Tests
# =============================================================================


class TestShareScore(unittest.TestCase):
    """Test _compute_share_score dimensions."""

    def test_01_clean_no_feedback(self):
        """Clean content with no feedback scores generalizability only."""
        skill = Skill(id="x", section="general", content="Run tests before committing")
        score = Skillbook._compute_share_score(skill)
        # generalizability=0.4 (no env refs), reusability=0 (no helpful), executability=0.15 (has "run")
        self.assertGreater(score, 0.3)
        self.assertLess(score, 0.8)

    def test_02_env_refs_reduce_score(self):
        """Environment references reduce generalizability."""
        skill = Skill(id="x", section="general", content="Check localhost:8080 and /Users/alice/project")
        score = Skillbook._compute_share_score(skill)
        # env_refs=2 → generalizability = 0.4 * max(0, 1-0.4) = 0.24
        self.assertLess(score, 0.5)

    def test_03_helpful_boosts_score(self):
        """High helpful count boosts reusability."""
        skill = Skill(id="x", section="general", content="Run tests before committing", helpful=5)
        score = Skillbook._compute_share_score(skill)
        # reusability = 0.3 * min(1.0, 5/3) = 0.3
        self.assertGreater(score, 0.5)

    def test_04_actionable_content(self):
        """Content with actions and conditions scores higher on executability."""
        skill = Skill(
            id="x", section="general",
            content="If tests fail, run the linter before creating a PR"
        )
        score = Skillbook._compute_share_score(skill)
        # has_actions (run, create) + has_conditions (if, before) → executability = 0.3
        self.assertGreater(score, 0.5)

    def test_05_score_range(self):
        """Score is always in [0.0, 1.0]."""
        for content in ["x", "a" * 200, "Run if delete update check verify when before after"]:
            skill = Skill(id="x", section="general", content=content, helpful=100)
            score = Skillbook._compute_share_score(skill)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)


# =============================================================================
# Promotion Decision Tests
# =============================================================================


class TestPromotionDecision(unittest.TestCase):
    """Test _should_promote_to_shared mode dispatch."""

    def _make_skill(self, content="Run tests before committing", helpful=0):
        return Skill(id="x", section="general", content=content, helpful=helpful)

    def test_01_sharing_disabled(self):
        """When share_skills_to_team=False, always private_only."""
        status, score, reason = Skillbook._should_promote_to_shared(
            self._make_skill(), share_skills_to_team=False, skill_share_mode="auto_safe", threshold=0.5,
        )
        self.assertEqual(status, "private_only")

    def test_02_hard_block_overrides(self):
        """Hard-blocked content returns blocked regardless of mode."""
        status, score, reason = Skillbook._should_promote_to_shared(
            self._make_skill(content="Use api_key=sk-xxx"),
            share_skills_to_team=True, skill_share_mode="auto_aggressive", threshold=0.0,
        )
        self.assertEqual(status, "blocked")
        self.assertEqual(reason, "contains_secret")

    def test_03_manual_produces_candidate(self):
        """Manual mode produces candidate, not promoted."""
        status, score, reason = Skillbook._should_promote_to_shared(
            self._make_skill(), share_skills_to_team=True, skill_share_mode="manual", threshold=0.5,
        )
        self.assertEqual(status, "candidate")
        self.assertIn("manual", reason)

    def test_04_auto_safe_needs_helpful(self):
        """auto_safe requires helpful>=2 even if score is high."""
        skill = self._make_skill(helpful=0)
        status, _, _ = Skillbook._should_promote_to_shared(
            skill, share_skills_to_team=True, skill_share_mode="auto_safe", threshold=0.0,
        )
        self.assertEqual(status, "candidate")  # not promoted (helpful < 2)

    def test_05_auto_safe_promotes_with_feedback(self):
        """auto_safe promotes when score>=threshold AND helpful>=2."""
        skill = self._make_skill(helpful=3)
        status, _, reason = Skillbook._should_promote_to_shared(
            skill, share_skills_to_team=True, skill_share_mode="auto_safe", threshold=0.0,
        )
        self.assertEqual(status, "promoted")
        self.assertIn("auto_safe", reason)

    def test_06_auto_aggressive_promotes(self):
        """auto_aggressive promotes when score>=threshold (no helpful requirement)."""
        skill = self._make_skill(helpful=0)
        status, _, reason = Skillbook._should_promote_to_shared(
            skill, share_skills_to_team=True, skill_share_mode="auto_aggressive", threshold=0.0,
        )
        self.assertEqual(status, "promoted")
        self.assertIn("auto_aggressive", reason)

    def test_07_auto_aggressive_below_threshold(self):
        """auto_aggressive below threshold returns candidate."""
        skill = self._make_skill(helpful=0)
        status, _, _ = Skillbook._should_promote_to_shared(
            skill, share_skills_to_team=True, skill_share_mode="auto_aggressive", threshold=0.99,
        )
        self.assertEqual(status, "candidate")


# =============================================================================
# Query Isolation Tests
# =============================================================================


class TestQueryIsolation(unittest.TestCase):
    """Test dual-read query isolation: private visible to owner, shared to tenant, cross-tenant invisible."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_scope_test_")
        self.storage = InMemoryStorage()
        self.sb = _make_skillbook(self.storage, self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_private_visible_to_owner(self):
        """Private skill is visible to its owner."""
        config = _make_config()
        skill = _run(self.sb.add_skill(
            section="general", content="private skill",
            tenant_id="team1", user_id="alice", _config=config,
        ))
        self.assertEqual(skill.scope, "private")

        results = _run(self.sb.search("private", tenant_id="team1", user_id="alice"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, skill.id)

    def test_02_private_invisible_to_other_user(self):
        """Private skill is NOT visible to another user in same tenant."""
        config = _make_config()
        _run(self.sb.add_skill(
            section="general", content="alice private",
            tenant_id="team1", user_id="alice", _config=config,
        ))

        results = _run(self.sb.search("alice private", tenant_id="team1", user_id="bob"))
        self.assertEqual(len(results), 0)

    def test_03_shared_visible_to_tenant(self):
        """Shared (promoted) skill is visible to all users in the same tenant."""
        config = _make_config(
            share_skills_to_team=True,
            skill_share_mode="auto_aggressive",
            skill_share_score_threshold=0.0,
        )
        skill = _run(self.sb.add_skill(
            section="general", content="Run tests before committing",
            tenant_id="team1", user_id="alice", _config=config,
        ))
        self.assertEqual(skill.scope, "shared")

        # Bob can see it
        results = _run(self.sb.search("tests", tenant_id="team1", user_id="bob"))
        ids = [r.id for r in results]
        self.assertIn(skill.id, ids)

    def test_04_cross_tenant_invisible(self):
        """Skills from tenant1 are NOT visible to tenant2."""
        config = _make_config()
        _run(self.sb.add_skill(
            section="general", content="team1 secret knowledge",
            tenant_id="team1", user_id="alice", _config=config,
        ))

        results = _run(self.sb.search("secret knowledge", tenant_id="team2", user_id="charlie"))
        self.assertEqual(len(results), 0)

    def test_05_stats_scope_isolated(self):
        """stats() only counts skills visible to the tenant/user."""
        config = _make_config()
        _run(self.sb.add_skill(section="general", content="t1 skill", tenant_id="t1", user_id="u1", _config=config))
        _run(self.sb.add_skill(section="general", content="t2 skill", tenant_id="t2", user_id="u2", _config=config))

        stats_t1 = _run(self.sb.stats(tenant_id="t1", user_id="u1"))
        self.assertEqual(stats_t1["total"], 1)

        stats_t2 = _run(self.sb.stats(tenant_id="t2", user_id="u2"))
        self.assertEqual(stats_t2["total"], 1)

    def test_06_get_by_section_scope_isolated(self):
        """get_by_section respects tenant/user scope."""
        config = _make_config()
        _run(self.sb.add_skill(section="strategies", content="t1 strat", tenant_id="t1", user_id="u1", _config=config))
        _run(self.sb.add_skill(section="strategies", content="t2 strat", tenant_id="t2", user_id="u2", _config=config))

        skills = _run(self.sb.get_by_section("strategies", tenant_id="t1", user_id="u1"))
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0].content, "t1 strat")

    def test_07_skill_id_globally_unique(self):
        """skill IDs are globally unique (uuid4), no overlap across tenants."""
        import uuid
        config = _make_config()
        s1 = _run(self.sb.add_skill(section="general", content="skill a", tenant_id="t1", user_id="u1", _config=config))
        s2 = _run(self.sb.add_skill(section="general", content="skill b", tenant_id="t2", user_id="u2", _config=config))

        # Both valid uuid4
        uuid.UUID(s1.id)
        uuid.UUID(s2.id)
        # Different
        self.assertNotEqual(s1.id, s2.id)


# =============================================================================
# Integration: add_skill with sharing config
# =============================================================================


class TestAddSkillWithSharingConfig(unittest.TestCase):
    """Test add_skill integrates sharing judgment correctly."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_share_test_")
        self.storage = InMemoryStorage()
        self.sb = _make_skillbook(self.storage, self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_default_config_private(self):
        """Default config (share_skills_to_team=False) creates private skill."""
        config = _make_config()
        skill = _run(self.sb.add_skill(
            section="general", content="test content",
            tenant_id="t", user_id="u", _config=config,
        ))
        self.assertEqual(skill.scope, "private")
        self.assertEqual(skill.share_status, "private_only")

    def test_02_manual_mode_candidate(self):
        """manual mode marks as candidate, scope stays private."""
        config = _make_config(share_skills_to_team=True, skill_share_mode="manual")
        skill = _run(self.sb.add_skill(
            section="general", content="Run tests before committing",
            tenant_id="t", user_id="u", _config=config,
        ))
        self.assertEqual(skill.scope, "private")
        self.assertEqual(skill.share_status, "candidate")
        self.assertGreater(skill.share_score, 0)

    def test_03_hard_block_prevents_sharing(self):
        """Hard-blocked content stays private+blocked even with sharing enabled."""
        config = _make_config(
            share_skills_to_team=True,
            skill_share_mode="auto_aggressive",
            skill_share_score_threshold=0.0,
        )
        skill = _run(self.sb.add_skill(
            section="general", content="Set password: hunter2",
            tenant_id="t", user_id="u", _config=config,
        ))
        self.assertEqual(skill.scope, "private")
        self.assertEqual(skill.share_status, "blocked")
        self.assertEqual(skill.share_reason, "contains_secret")

    def test_04_auto_aggressive_promotes(self):
        """auto_aggressive with low threshold promotes to shared."""
        config = _make_config(
            share_skills_to_team=True,
            skill_share_mode="auto_aggressive",
            skill_share_score_threshold=0.0,
        )
        skill = _run(self.sb.add_skill(
            section="general", content="Run tests before committing",
            tenant_id="t", user_id="u", _config=config,
        ))
        self.assertEqual(skill.scope, "shared")
        self.assertEqual(skill.share_status, "promoted")

    def test_05_scope_fields_persisted(self):
        """Scope fields are persisted to storage."""
        config = _make_config(share_skills_to_team=True, skill_share_mode="manual")
        skill = _run(self.sb.add_skill(
            section="general", content="Run tests before committing",
            tenant_id="t1", user_id="u1", _config=config,
        ))
        records = _run(self.storage.get("skillbooks", [skill.id]))
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["tenant_id"], "t1")
        self.assertEqual(r["owner_user_id"], "u1")
        self.assertEqual(r["scope"], "private")
        self.assertEqual(r["share_status"], "candidate")
        self.assertGreater(r["share_score"], 0)


# =============================================================================
# Authorization Enforcement Tests
# =============================================================================


class TestAuthorizationEnforcement(unittest.TestCase):
    """Test _check_ownership and write operation authorization."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_auth_test_")
        self.storage = InMemoryStorage()
        self.sb = _make_skillbook(self.storage, self.temp_dir)
        # Create a skill owned by alice
        config = _make_config()
        self.skill = _run(self.sb.add_skill(
            section="general", content="alice skill",
            tenant_id="team1", user_id="alice", _config=config,
        ))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _with_enforcement(self, enabled: bool):
        """Temporarily set ace_scope_enforcement_enabled via request context."""
        self._ace_tokens = set_request_ace_config(ace_scope_enforcement=enabled)

    def test_01_owner_can_update(self):
        """Owner can update their own skill even with enforcement enabled."""
        self._with_enforcement(True)
        skill = _run(self.sb.update_skill(
            self.skill.id, content="updated", user_id="alice",
        ))
        self.assertEqual(skill.content, "updated")

    def test_02_non_owner_blocked_update(self):
        """Non-owner is blocked from updating when enforcement enabled."""
        self._with_enforcement(True)
        with self.assertRaises(SkillAuthorizationError):
            _run(self.sb.update_skill(
                self.skill.id, content="hacked", user_id="bob",
            ))

    def test_03_non_owner_allowed_without_enforcement(self):
        """Non-owner can update when enforcement is disabled."""
        self._with_enforcement(False)
        skill = _run(self.sb.update_skill(
            self.skill.id, content="updated by bob", user_id="bob",
        ))
        self.assertEqual(skill.content, "updated by bob")

    def test_04_owner_can_tag(self):
        """Owner can tag their own skill."""
        self._with_enforcement(True)
        _run(self.sb.tag_skill(self.skill.id, "helpful", user_id="alice"))
        records = _run(self.storage.get("skillbooks", [self.skill.id]))
        self.assertEqual(records[0]["helpful"], 1)

    def test_05_non_owner_blocked_tag(self):
        """Non-owner is blocked from tagging when enforcement enabled."""
        self._with_enforcement(True)
        with self.assertRaises(SkillAuthorizationError):
            _run(self.sb.tag_skill(self.skill.id, "helpful", user_id="bob"))

    def test_06_owner_can_remove(self):
        """Owner can remove their own skill."""
        self._with_enforcement(True)
        _run(self.sb.remove_skill(self.skill.id, user_id="alice"))
        records = _run(self.storage.get("skillbooks", [self.skill.id]))
        self.assertEqual(len(records), 0)

    def test_07_non_owner_blocked_remove(self):
        """Non-owner is blocked from removing when enforcement enabled."""
        self._with_enforcement(True)
        with self.assertRaises(SkillAuthorizationError):
            _run(self.sb.remove_skill(self.skill.id, user_id="bob"))

    def test_08_no_user_id_skips_check(self):
        """When user_id is empty, ownership check is skipped."""
        self._with_enforcement(True)
        skill = _run(self.sb.update_skill(
            self.skill.id, content="updated anonymously", user_id="",
        ))
        self.assertEqual(skill.content, "updated anonymously")


# =============================================================================
# Approval & Demotion Tests
# =============================================================================


class TestApprovalAndDemotion(unittest.TestCase):
    """Test list_candidates, review_skill, demote_skill."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_approval_test_")
        self.storage = InMemoryStorage()
        self.sb = _make_skillbook(self.storage, self.temp_dir)
        # Create a candidate skill (manual mode + sharing enabled)
        self.config = _make_config(share_skills_to_team=True, skill_share_mode="manual")
        self.candidate = _run(self.sb.add_skill(
            section="general", content="Run tests before committing",
            tenant_id="team1", user_id="alice", _config=self.config,
        ))
        self.assertEqual(self.candidate.share_status, "candidate")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_list_candidates(self):
        """list_candidates returns candidate skills for a tenant."""
        candidates = _run(self.sb.list_candidates(tenant_id="team1"))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].id, self.candidate.id)

    def test_02_list_candidates_empty_other_tenant(self):
        """list_candidates for another tenant returns empty."""
        candidates = _run(self.sb.list_candidates(tenant_id="team2"))
        self.assertEqual(len(candidates), 0)

    def test_03_approve_candidate(self):
        """Approving a candidate promotes it to shared."""
        skill = _run(self.sb.review_skill(
            self.candidate.id, decision="approve", reviewer_user_id="admin",
        ))
        self.assertEqual(skill.scope, "shared")
        self.assertEqual(skill.share_status, "promoted")
        self.assertIn("approved_by_admin", skill.share_reason)

    def test_04_reject_candidate(self):
        """Rejecting a candidate resets it to private_only."""
        skill = _run(self.sb.review_skill(
            self.candidate.id, decision="reject", reviewer_user_id="admin",
        ))
        self.assertEqual(skill.scope, "private")
        self.assertEqual(skill.share_status, "private_only")
        self.assertIn("rejected", skill.share_reason)

    def test_05_review_non_candidate_fails(self):
        """Reviewing a non-candidate raises ValueError."""
        # First approve it
        _run(self.sb.review_skill(self.candidate.id, decision="approve"))
        # Try to review again (now promoted, not candidate)
        with self.assertRaises(ValueError) as ctx:
            _run(self.sb.review_skill(self.candidate.id, decision="approve"))
        self.assertIn("not a candidate", str(ctx.exception))

    def test_06_invalid_decision_fails(self):
        """Invalid decision raises ValueError."""
        with self.assertRaises(ValueError) as ctx:
            _run(self.sb.review_skill(self.candidate.id, decision="maybe"))
        self.assertIn("Invalid decision", str(ctx.exception))

    def test_07_demote_shared_skill(self):
        """demote_skill changes a promoted skill back to private."""
        # Approve first
        _run(self.sb.review_skill(self.candidate.id, decision="approve"))
        # Demote
        tokens = set_request_ace_config(ace_scope_enforcement=False)
        skill = _run(self.sb.demote_skill(
            self.candidate.id, reason="no longer relevant",
            tenant_id="team1", user_id="alice",
        ))
        reset_request_ace_config(tokens)
        self.assertEqual(skill.scope, "private")
        self.assertEqual(skill.share_status, "demoted")
        self.assertEqual(skill.share_reason, "no longer relevant")

    def test_08_demote_non_shared_fails(self):
        """Demoting a non-shared skill raises ValueError."""
        # Candidate is not shared
        with self.assertRaises(ValueError) as ctx:
            _run(self.sb.demote_skill(
                self.candidate.id, reason="test",
                tenant_id="team1", user_id="alice",
            ))
        self.assertIn("not shared/promoted", str(ctx.exception))

    def test_09_demote_blocked_by_enforcement(self):
        """Non-owner blocked from demoting when enforcement enabled."""
        # Approve first
        _run(self.sb.review_skill(self.candidate.id, decision="approve"))
        # Enable enforcement
        tokens = set_request_ace_config(ace_scope_enforcement=True)
        try:
            with self.assertRaises(SkillAuthorizationError):
                _run(self.sb.demote_skill(
                    self.candidate.id, reason="test",
                    tenant_id="team1", user_id="bob",
                ))
        finally:
            reset_request_ace_config(tokens)

    def test_10_approved_skill_visible_to_tenant(self):
        """After approval, the skill is visible to all users in the tenant."""
        _run(self.sb.review_skill(self.candidate.id, decision="approve"))
        # Bob can now see it
        results = _run(self.sb.search("tests", tenant_id="team1", user_id="bob"))
        ids = [r.id for r in results]
        self.assertIn(self.candidate.id, ids)

    def test_11_demoted_skill_invisible_to_others(self):
        """After demotion, the skill is only visible to the owner again."""
        # Approve then demote
        _run(self.sb.review_skill(self.candidate.id, decision="approve"))
        tokens = set_request_ace_config(ace_scope_enforcement=False)
        _run(self.sb.demote_skill(
            self.candidate.id, reason="test",
            tenant_id="team1", user_id="alice",
        ))
        reset_request_ace_config(tokens)
        # Bob can no longer see it
        results = _run(self.sb.search("tests", tenant_id="team1", user_id="bob"))
        ids = [r.id for r in results]
        self.assertNotIn(self.candidate.id, ids)

        # Alice can still see it (private, owned by her)
        results = _run(self.sb.search("tests", tenant_id="team1", user_id="alice"))
        ids = [r.id for r in results]
        self.assertIn(self.candidate.id, ids)


# =============================================================================
# Legacy Handling & Migration Tests
# =============================================================================


class TestLegacyHandling(unittest.TestCase):
    """Test legacy scope visibility and migration."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_legacy_test_")
        self.storage = InMemoryStorage()
        self.sb = _make_skillbook(self.storage, self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _insert_legacy_skill(self, content, tenant_id="team1"):
        """Manually insert a skill record with no scope fields (simulates pre-migration data)."""
        from opencortex.models.embedder.base import EmbedResult
        skill_id = str(uuid4())
        embedder = MockEmbedder()
        vec = embedder.embed(content).dense_vector
        _run(self.storage.upsert("skillbooks", {
            "id": skill_id,
            "uri": f"opencortex://{tenant_id}/user/default/skillbooks/general/{skill_id}",
            "abstract": content,
            "context_type": "ace_skill",
            "type": "general",
            "vector": vec,
            "active_count": 0,
            "is_leaf": True,
            "helpful": 0,
            "harmful": 0,
            "neutral": 0,
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            # No scope fields — simulates legacy data
            "tenant_id": tenant_id,
            "owner_user_id": "",
            "scope": "",
            "share_status": "",
            "share_score": 0.0,
            "share_reason": "",
        }))
        return skill_id

    def test_01_legacy_skill_invisible_before_migration(self):
        """Legacy skill (scope='') is NOT visible in scoped queries."""
        sid = self._insert_legacy_skill("legacy knowledge")
        results = _run(self.sb.search("legacy", tenant_id="team1", user_id="alice"))
        ids = [r.id for r in results]
        # scope="" doesn't match shared/legacy/private — invisible
        self.assertNotIn(sid, ids)

    def test_02_migrate_sets_legacy_scope(self):
        """migrate_legacy_skills sets scope='legacy' on unscoped skills."""
        sid = self._insert_legacy_skill("old skill")
        result = _run(self.sb.migrate_legacy_skills(
            tenant_id="team1", owner_user_id="default_owner",
        ))
        self.assertEqual(result["migrated"], 1)
        self.assertEqual(result["skipped"], 0)

        records = _run(self.storage.get("skillbooks", [sid]))
        self.assertEqual(records[0]["scope"], "legacy")
        self.assertEqual(records[0]["share_status"], "private_only")
        self.assertEqual(records[0]["owner_user_id"], "default_owner")

    def test_03_legacy_visible_after_migration(self):
        """After migration, legacy skills are visible to all tenant users."""
        sid = self._insert_legacy_skill("migrated knowledge")
        _run(self.sb.migrate_legacy_skills(tenant_id="team1", owner_user_id="admin"))

        # Alice can see it
        results = _run(self.sb.search("migrated", tenant_id="team1", user_id="alice"))
        ids = [r.id for r in results]
        self.assertIn(sid, ids)

        # Bob can see it too
        results = _run(self.sb.search("migrated", tenant_id="team1", user_id="bob"))
        ids = [r.id for r in results]
        self.assertIn(sid, ids)

    def test_04_migration_skips_already_scoped(self):
        """Skills with existing scope are not modified by migration."""
        config = _make_config()
        skill = _run(self.sb.add_skill(
            section="general", content="already scoped",
            tenant_id="team1", user_id="alice", _config=config,
        ))
        # Also add a legacy skill
        self._insert_legacy_skill("unscoped skill")

        result = _run(self.sb.migrate_legacy_skills(
            tenant_id="team1", owner_user_id="admin",
        ))
        self.assertEqual(result["migrated"], 1)
        self.assertEqual(result["skipped"], 1)

        # Original skill unchanged
        records = _run(self.storage.get("skillbooks", [skill.id]))
        self.assertEqual(records[0]["scope"], "private")
        self.assertEqual(records[0]["owner_user_id"], "alice")

    def test_05_cross_tenant_legacy_invisible(self):
        """Legacy skills from team1 are NOT visible to team2."""
        sid = self._insert_legacy_skill("team1 legacy", tenant_id="team1")
        _run(self.sb.migrate_legacy_skills(tenant_id="team1", owner_user_id="admin"))

        results = _run(self.sb.search("legacy", tenant_id="team2", user_id="charlie"))
        ids = [r.id for r in results]
        self.assertNotIn(sid, ids)

    def test_06_migration_preserves_existing_tenant(self):
        """Migration uses existing tenant_id if already set on record."""
        sid = self._insert_legacy_skill("has tenant", tenant_id="team1")
        _run(self.sb.migrate_legacy_skills(
            tenant_id="fallback_team", owner_user_id="admin",
        ))
        records = _run(self.storage.get("skillbooks", [sid]))
        # Should keep "team1", not overwrite with "fallback_team"
        self.assertEqual(records[0]["tenant_id"], "team1")

    def test_07_idempotent_migration(self):
        """Running migration twice doesn't re-migrate already migrated skills."""
        self._insert_legacy_skill("once is enough")
        r1 = _run(self.sb.migrate_legacy_skills(tenant_id="t1", owner_user_id="u1"))
        self.assertEqual(r1["migrated"], 1)

        r2 = _run(self.sb.migrate_legacy_skills(tenant_id="t1", owner_user_id="u1"))
        self.assertEqual(r2["migrated"], 0)
        self.assertEqual(r2["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
