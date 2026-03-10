"""Tests for Skillbook → Knowledge Store migration."""

import unittest
from unittest.mock import AsyncMock, MagicMock

from opencortex.migration.v040_skillbook_to_knowledge import (
    classify_skill,
    map_scope,
    map_status,
    skill_to_knowledge,
    migrate_skillbook_to_knowledge,
)


class TestClassifySkill(unittest.TestCase):

    def test_action_template_is_sop(self):
        record = {"action_template": ["step1", "step2"], "content": "do X"}
        self.assertEqual(classify_skill(record), "sop")

    def test_empty_action_template_with_steps_in_content(self):
        record = {"content": "1. Open file\n2. Edit line\n3. Save"}
        self.assertEqual(classify_skill(record), "sop")

    def test_plain_content_is_belief(self):
        record = {"content": "Always use type hints in Python"}
        self.assertEqual(classify_skill(record), "belief")

    def test_error_fixes_section(self):
        record = {"section": "error_fixes", "content": "ModuleNotFoundError means..."}
        self.assertEqual(classify_skill(record), "root_cause")

    def test_patterns_section(self):
        record = {"section": "patterns", "content": "Use async for I/O"}
        self.assertEqual(classify_skill(record), "belief")


class TestMapScope(unittest.TestCase):

    def test_private_to_user(self):
        self.assertEqual(map_scope("private"), "user")

    def test_shared_to_tenant(self):
        self.assertEqual(map_scope("shared"), "tenant")

    def test_legacy_to_user(self):
        self.assertEqual(map_scope("legacy"), "user")

    def test_unknown_to_user(self):
        self.assertEqual(map_scope("unknown"), "user")


class TestMapStatus(unittest.TestCase):

    def test_active_stays(self):
        self.assertEqual(map_status("active"), "active")

    def test_protected_to_active(self):
        self.assertEqual(map_status("protected"), "active")

    def test_observation_to_candidate(self):
        self.assertEqual(map_status("observation"), "candidate")

    def test_deprecated_stays(self):
        self.assertEqual(map_status("deprecated"), "deprecated")

    def test_invalid_to_deprecated(self):
        self.assertEqual(map_status("invalid"), "deprecated")


class TestSkillToKnowledge(unittest.TestCase):

    def test_belief_mapping(self):
        record = {
            "id": "sk1",
            "content": "Always use type hints",
            "tenant_id": "team",
            "owner_user_id": "hugo",
            "scope": "private",
            "status": "active",
            "confidence_score": 0.8,
            "justification": "Improves readability",
        }
        k = skill_to_knowledge(record)
        self.assertEqual(k["knowledge_type"], "belief")
        self.assertEqual(k["knowledge_id"], "migrated-sk1")
        self.assertEqual(k["tenant_id"], "team")
        self.assertEqual(k["user_id"], "hugo")
        self.assertEqual(k["scope"], "user")
        self.assertEqual(k["status"], "active")
        self.assertEqual(k["confidence"], 0.8)
        self.assertEqual(k["statement"], "Always use type hints")
        self.assertIn("Justification", k["overview"])

    def test_sop_mapping(self):
        record = {
            "id": "sk2",
            "content": "Deploy procedure",
            "action_template": ["build", "test", "deploy"],
            "trigger_conditions": ["deploy", "release"],
            "success_metric": "No errors in 5 min",
            "tenant_id": "team",
            "owner_user_id": "hugo",
            "scope": "shared",
            "status": "active",
        }
        k = skill_to_knowledge(record)
        self.assertEqual(k["knowledge_type"], "sop")
        self.assertEqual(k["scope"], "tenant")
        self.assertEqual(k["action_steps"], ["build", "test", "deploy"])
        self.assertEqual(k["trigger_keywords"], ["deploy", "release"])
        self.assertEqual(k["success_criteria"], "No errors in 5 min")

    def test_root_cause_mapping(self):
        record = {
            "id": "sk3",
            "section": "error_fixes",
            "content": "ModuleNotFoundError: no module named X",
            "justification": "Missing pip install",
            "tenant_id": "team",
            "owner_user_id": "hugo",
            "scope": "private",
            "status": "active",
        }
        k = skill_to_knowledge(record)
        self.assertEqual(k["knowledge_type"], "root_cause")
        self.assertEqual(k["error_pattern"], "ModuleNotFoundError: no module named X")
        self.assertEqual(k["cause"], "Missing pip install")

    def test_deprecated_skill(self):
        record = {
            "id": "sk4",
            "content": "Old approach",
            "status": "deprecated",
            "tenant_id": "team",
            "owner_user_id": "hugo",
            "scope": "private",
        }
        k = skill_to_knowledge(record)
        self.assertEqual(k["status"], "deprecated")


class TestMigrateAsync(unittest.IsolatedAsyncioTestCase):

    def _make_storage(self, skills):
        storage = AsyncMock()
        storage.collection_exists = AsyncMock(return_value=True)
        storage.scroll = AsyncMock(return_value=skills)
        storage.get = AsyncMock(return_value=[])  # nothing migrated yet
        storage.upsert = AsyncMock()
        return storage

    def _make_embedder(self):
        embedder = MagicMock()
        embedder.embed = MagicMock(return_value=MagicMock(dense_vector=[0.1] * 4))
        return embedder

    async def test_migrate_two_skills(self):
        skills = [
            {"id": "s1", "content": "Use async", "tenant_id": "t", "owner_user_id": "u",
             "scope": "private", "status": "active"},
            {"id": "s2", "content": "Deploy steps", "action_template": ["a", "b"],
             "tenant_id": "t", "owner_user_id": "u", "scope": "shared", "status": "active"},
        ]
        storage = self._make_storage(skills)
        embedder = self._make_embedder()
        stats = await migrate_skillbook_to_knowledge(
            storage, embedder, embedding_dim=4,
        )
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["migrated"], 2)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(storage.upsert.call_count, 2)

    async def test_skip_already_migrated(self):
        skills = [
            {"id": "s1", "content": "Use async", "tenant_id": "t", "owner_user_id": "u",
             "scope": "private", "status": "active"},
        ]
        storage = self._make_storage(skills)
        # Simulate already migrated
        storage.get = AsyncMock(return_value=[{"knowledge_id": "migrated-s1"}])
        embedder = self._make_embedder()
        stats = await migrate_skillbook_to_knowledge(
            storage, embedder, embedding_dim=4,
        )
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["migrated"], 0)
        storage.upsert.assert_not_called()

    async def test_dry_run(self):
        skills = [
            {"id": "s1", "content": "test", "tenant_id": "t", "owner_user_id": "u",
             "scope": "private", "status": "active"},
        ]
        storage = self._make_storage(skills)
        embedder = self._make_embedder()
        stats = await migrate_skillbook_to_knowledge(
            storage, embedder, embedding_dim=4, dry_run=True,
        )
        self.assertEqual(stats["migrated"], 1)
        storage.upsert.assert_not_called()  # dry run doesn't write

    async def test_handles_error_gracefully(self):
        skills = [
            {"id": "s1", "content": "test", "tenant_id": "t", "owner_user_id": "u",
             "scope": "private", "status": "active"},
        ]
        storage = self._make_storage(skills)
        storage.upsert = AsyncMock(side_effect=Exception("write failed"))
        embedder = self._make_embedder()
        stats = await migrate_skillbook_to_knowledge(
            storage, embedder, embedding_dim=4,
        )
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["migrated"], 0)

    async def test_empty_collection(self):
        storage = self._make_storage([])
        embedder = self._make_embedder()
        stats = await migrate_skillbook_to_knowledge(
            storage, embedder, embedding_dim=4,
        )
        self.assertEqual(stats["total"], 0)


if __name__ == "__main__":
    unittest.main()
