import unittest
from opencortex.skill_engine.types import (
    SkillEvent, QualityCheck, QualityReport, TDDResult, SkillRating,
    SkillRecord, SkillCategory,
)


class TestSkillEvent(unittest.TestCase):
    def test_create_selected(self):
        e = SkillEvent(event_id="ev1", session_id="s1", turn_id="t1",
                       skill_id="sk-001", skill_uri="opencortex://t/u/skills/workflow/sk-001",
                       tenant_id="team1", user_id="hugo", event_type="selected")
        self.assertEqual(e.event_type, "selected")
        self.assertFalse(e.evaluated)
        d = e.to_dict()
        self.assertEqual(d["event_type"], "selected")

    def test_create_cited(self):
        e = SkillEvent(event_id="ev2", session_id="s1", turn_id="t2",
                       skill_id="sk-001", skill_uri="uri", tenant_id="t",
                       user_id="u", event_type="cited", outcome="success")
        self.assertEqual(e.outcome, "success")


class TestQualityReport(unittest.TestCase):
    def test_report(self):
        checks = [
            QualityCheck(name="name_format", severity="ERROR", passed=True, message="OK"),
            QualityCheck(name="content", severity="ERROR", passed=False, message="Short"),
        ]
        r = QualityReport(score=80, checks=checks, errors=1, warnings=0)
        self.assertEqual(r.errors, 1)


class TestSkillRating(unittest.TestCase):
    def test_compute_overall_a(self):
        r = SkillRating(practicality=8, clarity=7, automation=6, quality=9, impact=5)
        r.compute_overall()
        self.assertAlmostEqual(r.overall, 7.0)
        self.assertEqual(r.rank, "A")

    def test_rank_s(self):
        r = SkillRating(practicality=9.5, clarity=9, automation=9.5, quality=9, impact=9)
        r.compute_overall()
        self.assertEqual(r.rank, "S")

    def test_rank_c(self):
        r = SkillRating(practicality=2, clarity=3, automation=1, quality=2, impact=1)
        r.compute_overall()
        self.assertEqual(r.rank, "C")

    def test_to_dict(self):
        r = SkillRating(practicality=5.0)
        d = r.to_dict()
        self.assertEqual(d["practicality"], 5.0)
        self.assertIn("rank", d)


class TestSkillRecordNewFields(unittest.TestCase):
    def test_defaults(self):
        r = SkillRecord(skill_id="sk-001", name="test", description="d",
                        content="c", category=SkillCategory.WORKFLOW,
                        tenant_id="t", user_id="u")
        self.assertEqual(r.quality_score, 0)
        self.assertFalse(r.tdd_passed)
        self.assertEqual(r.reward_score, 0.0)
        self.assertIsInstance(r.rating, SkillRating)

    def test_to_dict(self):
        r = SkillRecord(skill_id="sk-001", name="test", description="d",
                        content="c", category=SkillCategory.WORKFLOW,
                        tenant_id="t", user_id="u",
                        quality_score=85, tdd_passed=True, reward_score=0.5)
        d = r.to_dict()
        self.assertEqual(d["quality_score"], 85)
        self.assertTrue(d["tdd_passed"])
        self.assertEqual(d["reward_score"], 0.5)
        self.assertIn("rating", d)


class TestTDDResult(unittest.TestCase):
    def test_create(self):
        r = TDDResult(passed=True, scenarios_total=3, scenarios_improved=2,
                      scenarios_same=1, scenarios_worse=0,
                      sections_cited=["Steps"], quality_delta=0.67, llm_calls_used=7)
        self.assertTrue(r.passed)
        self.assertEqual(r.llm_calls_used, 7)


if __name__ == "__main__":
    unittest.main()
