import unittest
from unittest.mock import AsyncMock
from opencortex.skill_engine.types import SkillEvent
from opencortex.skill_engine.event_store import SkillEventStore


class TestSkillEventStore(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.storage = AsyncMock()
        self.storage.create_collection = AsyncMock(return_value=True)
        self.store = SkillEventStore(storage=self.storage)

    def _make_event(self, event_id="ev1", event_type="selected", evaluated=False):
        return SkillEvent(
            event_id=event_id, session_id="s1", turn_id="t1",
            skill_id="sk-001", skill_uri="opencortex://t/u/skills/workflow/sk-001",
            tenant_id="team1", user_id="hugo",
            event_type=event_type, evaluated=evaluated,
        )

    async def test_append(self):
        await self.store.append(self._make_event())
        self.storage.upsert.assert_called_once()
        payload = self.storage.upsert.call_args[0][1]
        self.assertEqual(payload["id"], "ev1")
        self.assertEqual(payload["event_type"], "selected")

    async def test_list_by_session(self):
        self.storage.filter = AsyncMock(return_value=[self._make_event().to_dict()])
        events = await self.store.list_by_session("s1", "team1", "hugo")
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], SkillEvent)
        self.assertEqual(events[0].event_id, "ev1")

    async def test_list_by_session_filters_by_user(self):
        """Verify the filter includes user_id."""
        self.storage.filter = AsyncMock(return_value=[])
        await self.store.list_by_session("s1", "team1", "hugo")
        filter_expr = self.storage.filter.call_args[0][1]
        conds_str = str(filter_expr)
        self.assertIn("user_id", conds_str)
        self.assertIn("hugo", conds_str)

    async def test_mark_evaluated(self):
        await self.store.mark_evaluated(["ev1", "ev2"])
        self.assertEqual(self.storage.update.call_count, 2)
        self.storage.update.assert_any_call("skill_events", "ev1", {"evaluated": True})

    async def test_list_unevaluated(self):
        self.storage.filter = AsyncMock(return_value=[
            {**self._make_event().to_dict(), "evaluated": False},
        ])
        events = await self.store.list_unevaluated("team1")
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].evaluated)


if __name__ == "__main__":
    unittest.main()
