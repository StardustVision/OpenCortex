import unittest
from opencortex.skill_engine.types import SkillRecord, SkillCategory, TDDResult
from opencortex.skill_engine.sandbox_tdd import SandboxTDD


class TestSandboxTDD(unittest.IsolatedAsyncioTestCase):

    def _make_skill(self):
        return SkillRecord(
            skill_id="sk-001", name="deploy-flow",
            description="Standard deploy workflow",
            content="# Deploy\n\n1. Build the project\n2. Run all tests\n3. Deploy to staging\n4. Verify health",
            category=SkillCategory.WORKFLOW, tenant_id="t", user_id="u",
        )

    async def test_passes_when_improves(self):
        async def mock_llm(msgs):
            content = msgs[-1]["content"] if msgs else ""
            if "Generate 2-3" in content:
                return '[{"scenario": "Deploy under pressure", "correct": "A"}]'
            if "operational skill" in content:
                return '{"choice": "A", "reasoning": "Following skill", "sections_cited": ["Steps"]}'
            return '{"choice": "B", "reasoning": "Just deploy"}'
        result = await SandboxTDD(llm=mock_llm, max_llm_calls=20).evaluate(self._make_skill())
        self.assertTrue(result.passed)
        self.assertGreater(result.scenarios_improved, 0)

    async def test_fails_when_worse(self):
        async def mock_llm(msgs):
            content = msgs[-1]["content"] if msgs else ""
            if "Generate 2-3" in content:
                return '[{"scenario": "Deploy scenario", "correct": "A"}]'
            if "operational skill" in content:
                return '{"choice": "C", "reasoning": "Confused"}'
            return '{"choice": "A", "reasoning": "Common sense"}'
        result = await SandboxTDD(llm=mock_llm, max_llm_calls=20).evaluate(self._make_skill())
        self.assertFalse(result.passed)
        self.assertGreater(result.scenarios_worse, 0)

    async def test_budget_respected(self):
        call_count = 0
        async def mock_llm(msgs):
            nonlocal call_count; call_count += 1
            return '[]'
        await SandboxTDD(llm=mock_llm, max_llm_calls=3).evaluate(self._make_skill())
        self.assertLessEqual(call_count, 3)

    async def test_handles_llm_error(self):
        async def mock_llm(msgs):
            raise Exception("LLM down")
        result = await SandboxTDD(llm=mock_llm, max_llm_calls=20).evaluate(self._make_skill())
        self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
