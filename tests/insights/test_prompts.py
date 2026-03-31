"""Tests for insights prompt templates."""

import pytest
from opencortex.insights.prompts import (
    FACET_EXTRACTION_PROMPT,
    CHUNK_SUMMARY_PROMPT,
    PROJECT_AREAS_PROMPT,
    WHAT_WORKS_PROMPT,
    FRICTION_ANALYSIS_PROMPT,
    SUGGESTIONS_PROMPT,
    ON_THE_HORIZON_PROMPT,
    AT_A_GLANCE_PROMPT,
)


class TestPromptTemplates:
    """Test prompt templates are properly formatted and have placeholders."""

    def test_facet_extraction_prompt_has_placeholders(self):
        """FACET_EXTRACTION_PROMPT contains required placeholders."""
        assert FACET_EXTRACTION_PROMPT is not None
        assert isinstance(FACET_EXTRACTION_PROMPT, str)
        assert "{session_transcript}" in FACET_EXTRACTION_PROMPT
        assert len(FACET_EXTRACTION_PROMPT) > 50

    def test_chunk_summary_prompt_has_placeholders(self):
        """CHUNK_SUMMARY_PROMPT contains required placeholders."""
        assert CHUNK_SUMMARY_PROMPT is not None
        assert isinstance(CHUNK_SUMMARY_PROMPT, str)
        assert "{chunk_content}" in CHUNK_SUMMARY_PROMPT
        assert len(CHUNK_SUMMARY_PROMPT) > 50

    def test_project_areas_prompt_has_placeholders(self):
        """PROJECT_AREAS_PROMPT contains required placeholders."""
        assert PROJECT_AREAS_PROMPT is not None
        assert isinstance(PROJECT_AREAS_PROMPT, str)
        assert "{sessions_summary}" in PROJECT_AREAS_PROMPT
        assert len(PROJECT_AREAS_PROMPT) > 50

    def test_what_works_prompt_has_placeholders(self):
        """WHAT_WORKS_PROMPT contains required placeholders."""
        assert WHAT_WORKS_PROMPT is not None
        assert isinstance(WHAT_WORKS_PROMPT, str)
        assert "{session_data}" in WHAT_WORKS_PROMPT
        assert len(WHAT_WORKS_PROMPT) > 50

    def test_friction_analysis_prompt_has_placeholders(self):
        """FRICTION_ANALYSIS_PROMPT contains required placeholders."""
        assert FRICTION_ANALYSIS_PROMPT is not None
        assert isinstance(FRICTION_ANALYSIS_PROMPT, str)
        assert "{session_data}" in FRICTION_ANALYSIS_PROMPT
        assert len(FRICTION_ANALYSIS_PROMPT) > 50

    def test_suggestions_prompt_has_placeholders(self):
        """SUGGESTIONS_PROMPT contains required placeholders."""
        assert SUGGESTIONS_PROMPT is not None
        assert isinstance(SUGGESTIONS_PROMPT, str)
        assert "{findings}" in SUGGESTIONS_PROMPT
        assert len(SUGGESTIONS_PROMPT) > 50

    def test_on_the_horizon_prompt_has_placeholders(self):
        """ON_THE_HORIZON_PROMPT contains required placeholders."""
        assert ON_THE_HORIZON_PROMPT is not None
        assert isinstance(ON_THE_HORIZON_PROMPT, str)
        assert "{context}" in ON_THE_HORIZON_PROMPT
        assert len(ON_THE_HORIZON_PROMPT) > 50

    def test_at_a_glance_prompt_has_placeholders(self):
        """AT_A_GLANCE_PROMPT contains required placeholders."""
        assert AT_A_GLANCE_PROMPT is not None
        assert isinstance(AT_A_GLANCE_PROMPT, str)
        assert "{insights_data}" in AT_A_GLANCE_PROMPT
        assert len(AT_A_GLANCE_PROMPT) > 50

    def test_all_prompts_are_strings(self):
        """All prompts are valid string templates."""
        prompts = [
            FACET_EXTRACTION_PROMPT,
            CHUNK_SUMMARY_PROMPT,
            PROJECT_AREAS_PROMPT,
            WHAT_WORKS_PROMPT,
            FRICTION_ANALYSIS_PROMPT,
            SUGGESTIONS_PROMPT,
            ON_THE_HORIZON_PROMPT,
            AT_A_GLANCE_PROMPT,
        ]
        for prompt in prompts:
            assert isinstance(prompt, str)
            assert len(prompt) > 0
