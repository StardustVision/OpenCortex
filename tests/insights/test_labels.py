"""Tests for insights label mapping."""
import unittest
from opencortex.insights.labels import LABEL_MAP, label


class TestLabels(unittest.TestCase):
    def test_goal_categories(self):
        self.assertEqual(label("debug_investigate"), "Debug/Investigate")
        self.assertEqual(label("implement_feature"), "Implement Feature")
        self.assertEqual(label("warmup_minimal"), "Cache Warmup")

    def test_friction_types(self):
        self.assertEqual(label("misunderstood_request"), "Misunderstood Request")
        self.assertEqual(label("wrong_approach"), "Wrong Approach")
        self.assertEqual(label("excessive_changes"), "Excessive Changes")

    def test_satisfaction(self):
        self.assertEqual(label("frustrated"), "Frustrated")
        self.assertEqual(label("likely_satisfied"), "Likely Satisfied")
        self.assertEqual(label("delighted"), "Delighted")

    def test_outcomes(self):
        self.assertEqual(label("fully_achieved"), "Fully Achieved")
        self.assertEqual(label("unclear_from_transcript"), "Unclear")

    def test_helpfulness(self):
        self.assertEqual(label("essential"), "Essential")
        self.assertEqual(label("slightly_helpful"), "Slightly Helpful")

    def test_unknown_key_fallback(self):
        self.assertEqual(label("some_unknown_key"), "Some Unknown Key")

    def test_label_map_size(self):
        self.assertGreaterEqual(len(LABEL_MAP), 40)


if __name__ == "__main__":
    unittest.main()
