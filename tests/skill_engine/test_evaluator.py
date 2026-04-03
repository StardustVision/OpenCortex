import unittest
from unittest.mock import AsyncMock
from opencortex.skill_engine.types import SkillEvent
from opencortex.skill_engine.evaluator import SkillEvaluator


class TestSkillEvaluator(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.event_store = AsyncMock()
        self.skill_store = AsyncMock()
        self.trace_store = AsyncMock()
        self.skill_storage = AsyncMock()
        self.evaluator = SkillEvaluator(
            event_store=self.event_store,
            skill_store=self.skill_store,
            trace_store=self.trace_store,
            skill_storage=self.skill_storage,
        )

    def _make_event(self, event_type="selected", evaluated=False):
        return SkillEvent(
            event_id="ev1", session_id="s1", turn_id="t1",
            skill_id="sk-001", skill_uri="opencortex://t/u/skills/workflow/sk-001",
            tenant_id="team1", user_id="hugo",
            event_type=event_type, evaluated=evaluated,
        )

    async def test_skips_no_events(self):
        self.event_store.list_by_session = AsyncMock(return_value=[])
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.skill_store.record_selection.assert_not_called()

    async def test_skips_already_evaluated(self):
        self.event_store.list_by_session = AsyncMock(return_value=[
            self._make_event(evaluated=True),
        ])
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.skill_store.record_selection.assert_not_called()

    async def test_records_selection(self):
        self.event_store.list_by_session = AsyncMock(return_value=[
            self._make_event(event_type="selected"),
        ])
        self.trace_store.list_by_session = AsyncMock(return_value=[{"outcome": "success"}])
        self.event_store.mark_evaluated = AsyncMock()
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.skill_store.record_selection.assert_called_once_with("sk-001")

    async def test_records_application_success(self):
        self.event_store.list_by_session = AsyncMock(return_value=[
            self._make_event(event_type="cited"),
        ])
        self.trace_store.list_by_session = AsyncMock(return_value=[{"outcome": "success"}])
        self.event_store.mark_evaluated = AsyncMock()
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.skill_store.record_application.assert_called_once_with("sk-001", True)
        self.skill_storage.update_reward.assert_called_once_with("sk-001", 0.1)

    async def test_records_application_failure(self):
        self.event_store.list_by_session = AsyncMock(return_value=[
            self._make_event(event_type="cited"),
        ])
        self.trace_store.list_by_session = AsyncMock(return_value=[{"outcome": "failure"}])
        self.event_store.mark_evaluated = AsyncMock()
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.skill_store.record_application.assert_called_once_with("sk-001", False)
        self.skill_storage.update_reward.assert_called_once_with("sk-001", -0.05)

    async def test_marks_evaluated(self):
        self.event_store.list_by_session = AsyncMock(return_value=[
            self._make_event(event_type="selected"),
        ])
        self.trace_store.list_by_session = AsyncMock(return_value=[])
        self.event_store.mark_evaluated = AsyncMock()
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.event_store.mark_evaluated.assert_called_once()

    async def test_sweep_unevaluated(self):
        self.event_store.list_unevaluated = AsyncMock(return_value=[
            self._make_event(event_type="selected"),
        ])
        self.event_store.list_by_session = AsyncMock(return_value=[
            self._make_event(event_type="selected"),
        ])
        self.trace_store.list_by_session = AsyncMock(return_value=[])
        self.event_store.mark_evaluated = AsyncMock()
        count = await self.evaluator.sweep_unevaluated("team1")
        self.assertEqual(count, 1)

    async def test_sweep_empty(self):
        self.event_store.list_unevaluated = AsyncMock(return_value=[])
        count = await self.evaluator.sweep_unevaluated("team1")
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
