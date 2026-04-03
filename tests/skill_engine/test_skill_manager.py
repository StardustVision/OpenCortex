import unittest
from unittest.mock import AsyncMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, make_skill_uri,
)
from opencortex.skill_engine.skill_manager import SkillManager


class TestSkillManager(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.store = AsyncMock()
        self.manager = SkillManager(store=self.store)

    def _make_record(self, skill_id="sk-001", status=SkillStatus.ACTIVE):
        return SkillRecord(
            skill_id=skill_id, name="deploy-flow",
            description="Deploy workflow", content="# Deploy",
            category=SkillCategory.WORKFLOW, status=status,
            tenant_id="team1", user_id="hugo",
            uri=make_skill_uri("team1", "hugo", skill_id),
            abstract="Standard deployment workflow",
        )

    async def test_search_delegates_to_store(self):
        self.store.search = AsyncMock(return_value=[self._make_record()])
        results = await self.manager.search("deploy", "team1", "hugo")
        self.assertEqual(len(results), 1)
        self.store.search.assert_called_once()

    async def test_approve_activates_skill(self):
        await self.manager.approve("sk-001", "team1", "hugo")
        self.store.activate.assert_called_once_with("sk-001")

    async def test_reject_deprecates_skill(self):
        await self.manager.reject("sk-001", "team1", "hugo")
        self.store.deprecate.assert_called_once_with("sk-001")

    async def test_list_skills_with_status(self):
        self.store.load_by_status = AsyncMock(return_value=[])
        await self.manager.list_skills("team1", "hugo", status=SkillStatus.ACTIVE)
        self.store.load_by_status.assert_called_once()

    async def test_list_skills_default_returns_active_plus_candidate(self):
        self.store.load_by_status = AsyncMock(return_value=[])
        await self.manager.list_skills("team1", "hugo")
        self.assertEqual(self.store.load_by_status.call_count, 2)

    async def test_get_skill(self):
        self.store.load_record = AsyncMock(return_value=self._make_record())
        r = await self.manager.get_skill("sk-001", "team1", "hugo")
        self.assertIsNotNone(r)


if __name__ == "__main__":
    unittest.main()
