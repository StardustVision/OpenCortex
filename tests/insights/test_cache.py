"""Tests for InsightsCache (CortexFS-backed)."""
import json
import unittest
from dataclasses import asdict
from unittest.mock import AsyncMock

from opencortex.insights.cache import InsightsCache, _validate_facet
from opencortex.insights.types import SessionMeta, SessionFacet


class TestValidateFacet(unittest.TestCase):
    def test_valid(self):
        data = {
            "session_id": "s1", "underlying_goal": "g",
            "goal_categories": {}, "outcome": "fully_achieved",
            "brief_summary": "summary",
        }
        self.assertTrue(_validate_facet(data))

    def test_missing_field(self):
        data = {"session_id": "s1", "underlying_goal": "g"}
        self.assertFalse(_validate_facet(data))


class TestInsightsCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fs = AsyncMock()
        self.cache = InsightsCache(self.fs)

    async def test_put_and_get_meta(self):
        meta = SessionMeta(
            session_id="s1", tenant_id="t1", user_id="u1",
            project_path="", start_time="", duration_minutes=0,
            user_message_count=5, assistant_message_count=5,
            tool_counts={"Read": 3}, languages={"Python": 2},
            git_commits=1, git_pushes=0,
            input_tokens=100, output_tokens=200, first_prompt="hi",
        )
        self.fs.read = AsyncMock(return_value=json.dumps(asdict(meta)))

        await self.cache.put_meta("t1", "u1", "s1", meta)
        self.fs.write.assert_called_once()

        result = await self.cache.get_meta("t1", "u1", "s1")
        self.assertIsNotNone(result)
        self.assertEqual(result.session_id, "s1")

    async def test_get_meta_miss(self):
        self.fs.read = AsyncMock(return_value=None)
        result = await self.cache.get_meta("t1", "u1", "missing")
        self.assertIsNone(result)

    async def test_put_and_get_facet(self):
        facet = SessionFacet(
            session_id="s1", underlying_goal="goal",
            goal_categories={"implement_feature": 1},
            outcome="fully_achieved",
            user_satisfaction_counts={"satisfied": 1},
            claude_helpfulness="very_helpful",
            session_type="single_task",
            brief_summary="summary",
        )
        self.fs.read = AsyncMock(return_value=json.dumps(asdict(facet)))

        await self.cache.put_facet("t1", "u1", "s1", facet)
        self.fs.write.assert_called_once()

        result = await self.cache.get_facet("t1", "u1", "s1")
        self.assertIsNotNone(result)
        self.assertEqual(result.session_id, "s1")
        self.assertEqual(result.goal_categories["implement_feature"], 1)

    async def test_corrupted_facet_deleted(self):
        self.fs.read = AsyncMock(return_value='{"session_id": "s1"}')
        self.fs.delete = AsyncMock()
        result = await self.cache.get_facet("t1", "u1", "s1")
        self.assertIsNone(result)
        self.fs.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
