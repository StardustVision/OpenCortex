import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, SkillVisibility,
    make_skill_uri,
)
from opencortex.skill_engine.adapters.storage_adapter import SkillStorageAdapter


class TestSkillStorageAdapter(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.storage = AsyncMock()
        self.storage.collection_exists = AsyncMock(return_value=True)
        self.embedder = MagicMock()
        self.embedder.embed = MagicMock(
            return_value=MagicMock(dense_vector=[0.1] * 4)
        )
        self.adapter = SkillStorageAdapter(
            storage=self.storage, embedder=self.embedder,
            collection_name="skills", embedding_dim=4,
        )

    def _make_record(self, skill_id="sk-001", status=SkillStatus.CANDIDATE):
        return SkillRecord(
            skill_id=skill_id, name="deploy-flow",
            description="Deploy workflow", content="# Deploy",
            category=SkillCategory.WORKFLOW,
            status=status,
            tenant_id="team1", user_id="hugo",
            uri=make_skill_uri("team1", "hugo", skill_id),
        )

    async def test_save_calls_upsert(self):
        r = self._make_record()
        await self.adapter.save(r)
        self.storage.upsert.assert_called_once()
        call_args = self.storage.upsert.call_args
        self.assertEqual(call_args[0][0], "skills")
        payload = call_args[0][1]
        self.assertEqual(payload["id"], "sk-001")
        self.assertEqual(payload["status"], "candidate")
        self.assertEqual(payload["visibility"], "private")

    async def test_load_returns_record(self):
        self.storage.get = AsyncMock(return_value=[{
            "skill_id": "sk-001", "name": "test", "description": "d",
            "content": "c", "category": "workflow", "status": "active",
            "visibility": "private", "tenant_id": "t", "user_id": "u",
            "uri": "opencortex://t/u/skills/sk-001",
            "lineage": {"origin": "captured", "generation": 0},
        }])
        r = await self.adapter.load("sk-001")
        self.assertIsNotNone(r)
        self.assertEqual(r.skill_id, "sk-001")
        self.assertEqual(r.status, SkillStatus.ACTIVE)

    async def test_load_returns_none_for_missing(self):
        self.storage.get = AsyncMock(return_value=[])
        r = await self.adapter.load("nonexistent")
        self.assertIsNone(r)

    async def test_search_applies_visibility_filter(self):
        self.storage.search = AsyncMock(return_value=[])
        await self.adapter.search("deploy", "team1", "hugo", top_k=5)
        call_args = self.storage.search.call_args
        # filter is now passed as keyword arg
        filter_expr = call_args[1].get("filter") or call_args[0][2] if len(call_args[0]) > 2 else call_args[1]["filter"]
        self.assertEqual(filter_expr["op"], "and")
        self.assertTrue(len(filter_expr["conds"]) >= 2)

    async def test_find_by_fingerprint(self):
        self.storage.filter = AsyncMock(return_value=[{
            "skill_id": "sk-001", "name": "test", "description": "d",
            "content": "c", "category": "workflow", "status": "candidate",
            "visibility": "private", "tenant_id": "t", "user_id": "u",
            "source_fingerprint": "abc123",
            "lineage": {"origin": "captured"},
        }])
        r = await self.adapter.find_by_fingerprint("abc123")
        self.assertIsNotNone(r)
        self.assertEqual(r.source_fingerprint, "abc123")

    async def test_update_status(self):
        await self.adapter.update_status("sk-001", SkillStatus.ACTIVE)
        self.storage.update.assert_called_once()
        call_args = self.storage.update.call_args
        self.assertEqual(call_args[0][2]["status"], "active")


if __name__ == "__main__":
    unittest.main()
