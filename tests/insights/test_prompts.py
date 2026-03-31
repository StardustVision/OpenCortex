"""Tests for CC-equivalent insights prompts."""
import unittest
from opencortex.insights.prompts import (
    FACET_EXTRACTION_PROMPT,
    CHUNK_SUMMARY_PROMPT,
    PROJECT_AREAS_PROMPT,
    INTERACTION_STYLE_PROMPT,
    WHAT_WORKS_PROMPT,
    FRICTION_ANALYSIS_PROMPT,
    SUGGESTIONS_PROMPT,
    ON_THE_HORIZON_PROMPT,
    FUN_ENDING_PROMPT,
    AT_A_GLANCE_PROMPT,
)


class TestPrompts(unittest.TestCase):
    def test_all_ten_prompts_defined(self):
        prompts = [
            FACET_EXTRACTION_PROMPT, CHUNK_SUMMARY_PROMPT,
            PROJECT_AREAS_PROMPT, INTERACTION_STYLE_PROMPT,
            WHAT_WORKS_PROMPT, FRICTION_ANALYSIS_PROMPT,
            SUGGESTIONS_PROMPT, ON_THE_HORIZON_PROMPT,
            FUN_ENDING_PROMPT, AT_A_GLANCE_PROMPT,
        ]
        for p in prompts:
            self.assertIsInstance(p, str)
            self.assertGreater(len(p), 50)

    def test_facet_has_critical_guidelines(self):
        self.assertIn("CRITICAL GUIDELINES", FACET_EXTRACTION_PROMPT)
        self.assertIn("goal_categories", FACET_EXTRACTION_PROMPT)
        self.assertIn("{transcript}", FACET_EXTRACTION_PROMPT)
        self.assertIn("user_instructions_to_claude", FACET_EXTRACTION_PROMPT)

    def test_chunk_summary_placeholder(self):
        self.assertIn("{chunk}", CHUNK_SUMMARY_PROMPT)

    def test_section_prompts_have_data_context(self):
        for p in [PROJECT_AREAS_PROMPT, INTERACTION_STYLE_PROMPT,
                   WHAT_WORKS_PROMPT, FRICTION_ANALYSIS_PROMPT,
                   SUGGESTIONS_PROMPT, ON_THE_HORIZON_PROMPT,
                   FUN_ENDING_PROMPT]:
            self.assertIn("{data_context}", p)

    def test_at_a_glance_has_section_refs(self):
        self.assertIn("{project_areas_text}", AT_A_GLANCE_PROMPT)
        self.assertIn("{big_wins_text}", AT_A_GLANCE_PROMPT)
        self.assertIn("{friction_text}", AT_A_GLANCE_PROMPT)
        self.assertIn("{features_text}", AT_A_GLANCE_PROMPT)
        self.assertIn("{horizon_text}", AT_A_GLANCE_PROMPT)

    def test_suggestions_has_oc_features_reference(self):
        self.assertIn("OC FEATURES REFERENCE", SUGGESTIONS_PROMPT)
        self.assertIn("Memory Feedback", SUGGESTIONS_PROMPT)
        self.assertIn("Knowledge Pipeline", SUGGESTIONS_PROMPT)
        self.assertIn("Batch Import", SUGGESTIONS_PROMPT)

    def test_interaction_style_prompt_exists(self):
        self.assertIn("interaction style", INTERACTION_STYLE_PROMPT.lower())
        self.assertIn("narrative", INTERACTION_STYLE_PROMPT)

    def test_fun_ending_prompt_exists(self):
        self.assertIn("memorable", FUN_ENDING_PROMPT.lower())
        self.assertIn("headline", FUN_ENDING_PROMPT)


if __name__ == "__main__":
    unittest.main()
