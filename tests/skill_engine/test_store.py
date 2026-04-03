import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, SkillVisibility,
    SkillLineage, SkillOrigin, make_skill_uri,
)
from opencortex.skill_engine.store import SkillStore


class TestSkillStore(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.storage_adapter = AsyncMock()
        self.store = SkillStore(self.storage_adapter)

    def _make_record(self, skill_id="sk-001", status=SkillStatus.CANDIDATE):
        return SkillRecord(
            skill_id=skill_id, name="deploy-flow",
            description="Deploy workflow", content="# Deploy",
            category=SkillCategory.WORKFLOW, status=status,
            tenant_id="team1", user_id="hugo",
            uri=make_skill_uri("team1", "hugo", skill_id),
        )

    async def test_save_record(self):
        r = self._make_record()
        await self.store.save_record(r)
        self.storage_adapter.save.assert_called_once_with(r)

    async def test_load_record(self):
        r = self._make_record()
        self.storage_adapter.load = AsyncMock(return_value=r)
        result = await self.store.load_record("sk-001")
        self.assertEqual(result.skill_id, "sk-001")

    async def test_activate(self):
        await self.store.activate("sk-001")
        self.storage_adapter.update_status.assert_called_once_with(
            "sk-001", SkillStatus.ACTIVE,
        )

    async def test_deprecate(self):
        await self.store.deprecate("sk-001")
        self.storage_adapter.update_status.assert_called_once_with(
            "sk-001", SkillStatus.DEPRECATED,
        )

    async def test_evolve_skill_saves_new_and_deprecates_parents(self):
        new = self._make_record(skill_id="sk-002")
        new.lineage = SkillLineage(
            origin=SkillOrigin.FIXED,
            parent_skill_ids=["sk-001"],
        )
        await self.store.evolve_skill(new, parent_ids=["sk-001"])
        self.storage_adapter.save.assert_called_once_with(new)
        self.storage_adapter.update_status.assert_called_once_with(
            "sk-001", SkillStatus.DEPRECATED,
        )

    async def test_search_delegates(self):
        self.storage_adapter.search = AsyncMock(return_value=[])
        await self.store.search("deploy", "team1", "hugo", top_k=5)
        self.storage_adapter.search.assert_called_once_with(
            "deploy", "team1", "hugo", top_k=5,
        )

    async def test_find_by_fingerprint(self):
        self.storage_adapter.find_by_fingerprint = AsyncMock(return_value=None)
        result = await self.store.find_by_fingerprint("abc123")
        self.assertIsNone(result)

    async def test_record_selection(self):
        await self.store.record_selection("sk-001")
        self.storage_adapter.update_metrics.assert_called_once_with(
            "sk-001", total_selections=1,
        )

    async def test_record_application_completed(self):
        await self.store.record_application("sk-001", completed=True)
        self.storage_adapter.update_metrics.assert_called_once_with(
            "sk-001", total_applied=1, total_completions=1,
        )

    async def test_record_application_failed(self):
        await self.store.record_application("sk-001", completed=False)
        self.storage_adapter.update_metrics.assert_called_once_with(
            "sk-001", total_applied=1, total_fallbacks=1,
        )


if __name__ == "__main__":
    unittest.main()
