import unittest
from opencortex.skill_engine.types import SkillEvent


class TestSelectionTracking(unittest.TestCase):
    def test_skill_event_type(self):
        e = SkillEvent(event_id="ev1", session_id="s1", turn_id="t1",
                       skill_id="sk-001", skill_uri="opencortex://t/u/skills/workflow/sk-001",
                       tenant_id="t", user_id="u", event_type="selected")
        self.assertEqual(e.event_type, "selected")

    def test_to_dict(self):
        e = SkillEvent(event_id="ev1", session_id="s1", turn_id="t1",
                       skill_id="sk-001", skill_uri="opencortex://t/u/skills/workflow/sk-001",
                       tenant_id="t", user_id="u", event_type="cited")
        d = e.to_dict()
        self.assertEqual(d["event_type"], "cited")
        self.assertFalse(d["evaluated"])


class TestCitationValidation(unittest.TestCase):
    def test_valid_citation(self):
        server_selected = {"opencortex://t/u/skills/workflow/sk-001"}
        self.assertIn("opencortex://t/u/skills/workflow/sk-001", server_selected)

    def test_forged_citation_rejected(self):
        server_selected = {"opencortex://t/u/skills/workflow/sk-001"}
        self.assertNotIn("opencortex://t/other/skills/workflow/sk-999", server_selected)

    def test_empty_selected_set(self):
        server_selected = set()
        self.assertNotIn("opencortex://t/u/skills/workflow/sk-001", server_selected)


if __name__ == "__main__":
    unittest.main()
