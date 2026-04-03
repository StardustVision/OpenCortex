import unittest
from unittest.mock import AsyncMock
from opencortex.skill_engine.types import SkillRecord, SkillCategory, QualityReport
from opencortex.skill_engine.quality_gate import QualityGate


class TestQualityGateRules(unittest.TestCase):

    def _make_skill(self, name="deploy-flow",
                    content="# Deploy\n\n1. Build the application from source\n2. Test all modules\n3. Deploy to staging environment",
                    description="Standard deploy workflow"):
        return SkillRecord(
            skill_id="sk-001", name=name, description=description,
            content=content, category=SkillCategory.WORKFLOW,
            tenant_id="t", user_id="u",
        )

    def test_valid_skill_passes(self):
        report = QualityGate().rule_check(self._make_skill())
        self.assertEqual(report.errors, 0)
        self.assertGreaterEqual(report.score, 60)

    def test_empty_content_fails(self):
        report = QualityGate().rule_check(self._make_skill(content="short"))
        self.assertGreater(report.errors, 0)
        self.assertLessEqual(report.score, 60)

    def test_bad_name_fails(self):
        report = QualityGate().rule_check(self._make_skill(name="BAD NAME"))
        self.assertGreater(report.errors, 0)

    def test_no_steps_fails(self):
        report = QualityGate().rule_check(self._make_skill(
            content="# Deploy\n\nJust deploy it. No steps here, just a paragraph of text about deploying things."))
        self.assertGreater(report.errors, 0)

    def test_empty_description_fails(self):
        report = QualityGate().rule_check(self._make_skill(description=""))
        self.assertGreater(report.errors, 0)


class TestQualityGateEvaluate(unittest.IsolatedAsyncioTestCase):

    async def test_without_llm(self):
        report = await QualityGate(llm=None).evaluate(
            SkillRecord(skill_id="sk-001", name="deploy-flow",
                        description="Standard deploy",
                        content="# Deploy\n\n1. Build the application from source\n2. Test all modules\n3. Deploy to staging environment",
                        category=SkillCategory.WORKFLOW, tenant_id="t", user_id="u"))
        self.assertIsInstance(report, QualityReport)
        self.assertGreaterEqual(report.score, 60)

    async def test_with_llm(self):
        async def mock_llm(msgs):
            return '{"actionable": true, "consistent": true, "specific": true, "duplicate": false}'
        report = await QualityGate(llm=mock_llm).evaluate(
            SkillRecord(skill_id="sk-001", name="deploy-flow",
                        description="Standard deploy",
                        content="# Deploy\n\n1. Build the application from source\n2. Test all modules\n3. Deploy to staging environment",
                        category=SkillCategory.WORKFLOW, tenant_id="t", user_id="u"))
        self.assertGreaterEqual(report.score, 60)


if __name__ == "__main__":
    unittest.main()
