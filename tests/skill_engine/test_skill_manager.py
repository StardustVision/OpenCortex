import unittest
from unittest.mock import AsyncMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, SkillVisibility, make_skill_uri,
)
from opencortex.skill_engine.skill_manager import SkillManager


class TestSkillManager(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.store = AsyncMock()
        self.manager = SkillManager(store=self.store)

    def _make_record(self, skill_id="sk-001", status=SkillStatus.ACTIVE,
                     tenant_id="team1", user_id="hugo",
                     visibility=SkillVisibility.PRIVATE):
        return SkillRecord(
            skill_id=skill_id, name="deploy-flow",
            description="Deploy workflow", content="# Deploy",
            category=SkillCategory.WORKFLOW, status=status,
            visibility=visibility,
            tenant_id=tenant_id, user_id=user_id,
            uri=make_skill_uri(tenant_id, user_id, skill_id),
            abstract="Standard deployment workflow",
        )

    async def test_search_delegates_to_store(self):
        self.store.search = AsyncMock(return_value=[self._make_record()])
        results = await self.manager.search("deploy", "team1", "hugo")
        self.assertEqual(len(results), 1)
        self.store.search.assert_called_once()

    async def test_approve_loads_then_activates(self):
        """approve() loads record with visibility check before activating."""
        self.store.load_record = AsyncMock(return_value=self._make_record())
        await self.manager.approve("sk-001", "team1", "hugo")
        self.store.load_record.assert_called_once_with("sk-001")
        self.store.update_status.assert_called_once_with("sk-001", SkillStatus.ACTIVE)

    async def test_reject_loads_then_deprecates(self):
        """reject() loads record with visibility check before deprecating."""
        self.store.load_record = AsyncMock(return_value=self._make_record())
        await self.manager.reject("sk-001", "team1", "hugo")
        self.store.load_record.assert_called_once_with("sk-001")
        self.store.update_status.assert_called_once_with("sk-001", SkillStatus.DEPRECATED)

    async def test_approve_rejects_wrong_tenant(self):
        """approve() raises when skill belongs to different tenant."""
        self.store.load_record = AsyncMock(
            return_value=self._make_record(tenant_id="other-team")
        )
        with self.assertRaises(ValueError):
            await self.manager.approve("sk-001", "team1", "hugo")

    async def test_approve_rejects_private_wrong_user(self):
        """approve() raises when private skill belongs to different user."""
        self.store.load_record = AsyncMock(
            return_value=self._make_record(user_id="alice")
        )
        with self.assertRaises(ValueError):
            await self.manager.approve("sk-001", "team1", "hugo")

    async def test_approve_rejects_shared_skill_non_owner(self):
        """approve() rejects non-owner even for shared skills (write requires ownership)."""
        self.store.load_record = AsyncMock(
            return_value=self._make_record(
                user_id="alice", visibility=SkillVisibility.SHARED,
            )
        )
        with self.assertRaises(ValueError):
            await self.manager.approve("sk-001", "team1", "hugo")

    async def test_get_skill_returns_none_for_wrong_tenant(self):
        """get_skill() returns None when skill is in different tenant."""
        self.store.load_record = AsyncMock(
            return_value=self._make_record(tenant_id="other-team")
        )
        r = await self.manager.get_skill("sk-001", "team1", "hugo")
        self.assertIsNone(r)

    async def test_list_skills_with_status(self):
        self.store.load_by_status = AsyncMock(return_value=[])
        await self.manager.list_skills("team1", "hugo", status=SkillStatus.ACTIVE)
        self.store.load_by_status.assert_called_once()

    async def test_list_skills_default_returns_active_plus_candidate(self):
        self.store.load_by_status = AsyncMock(return_value=[])
        await self.manager.list_skills("team1", "hugo")
        self.assertEqual(self.store.load_by_status.call_count, 2)

    async def test_get_skill_visible(self):
        self.store.load_record = AsyncMock(return_value=self._make_record())
        r = await self.manager.get_skill("sk-001", "team1", "hugo")
        self.assertIsNotNone(r)

    async def test_extraction_available_false_by_default(self):
        self.assertFalse(self.manager.extraction_available)

    async def test_extract_raises_when_unavailable(self):
        with self.assertRaises(RuntimeError):
            await self.manager.extract("team1", "hugo")


    async def test_promote_changes_visibility(self):
        """promote() changes PRIVATE to SHARED."""
        r = self._make_record(visibility=SkillVisibility.PRIVATE)
        self.store.load_record = AsyncMock(return_value=r)
        self.store.update_visibility = AsyncMock()
        await self.manager.promote("sk-001", "team1", "hugo")
        self.store.update_visibility.assert_called_once()
        call_args = self.store.update_visibility.call_args
        self.assertEqual(call_args[0][1].value, "shared")

    async def test_promote_rejects_already_shared(self):
        """promote() raises when skill is already shared."""
        r = self._make_record(visibility=SkillVisibility.SHARED)
        self.store.load_record = AsyncMock(return_value=r)
        with self.assertRaises(ValueError):
            await self.manager.promote("sk-001", "team1", "hugo")

    async def test_promote_rejects_non_owner(self):
        """promote() raises when caller is not the owner."""
        r = self._make_record(user_id="alice")
        self.store.load_record = AsyncMock(return_value=r)
        with self.assertRaises(ValueError):
            await self.manager.promote("sk-001", "team1", "hugo")


if __name__ == "__main__":
    unittest.main()
