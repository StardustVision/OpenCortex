"""Tests for insights core data types (CC-equivalent)."""
import unittest
from dataclasses import asdict
from datetime import datetime
from opencortex.insights.types import (
    SessionMeta,
    SessionFacet,
    AggregatedData,
    InsightsReport,
)


class TestSessionMeta(unittest.TestCase):
    def test_creation_all_fields(self):
        meta = SessionMeta(
            session_id="s1", tenant_id="t1", user_id="u1",
            project_path="/project", start_time="2026-03-31T10:00:00",
            duration_minutes=30.0, user_message_count=10,
            assistant_message_count=12,
            tool_counts={"Read": 5, "Edit": 3},
            languages={"Python": 8},
            git_commits=2, git_pushes=1,
            input_tokens=5000, output_tokens=8000,
            first_prompt="Fix the auth bug",
        )
        self.assertEqual(meta.session_id, "s1")
        self.assertEqual(meta.tool_counts["Read"], 5)
        self.assertEqual(meta.git_commits, 2)

    def test_defaults(self):
        meta = SessionMeta(
            session_id="s1", tenant_id="t1", user_id="u1",
            project_path="", start_time="", duration_minutes=0,
            user_message_count=0, assistant_message_count=0,
            tool_counts={}, languages={},
            git_commits=0, git_pushes=0,
            input_tokens=0, output_tokens=0, first_prompt="",
        )
        self.assertEqual(meta.user_interruptions, 0)
        self.assertEqual(meta.user_response_times, [])
        self.assertFalse(meta.uses_agent)
        self.assertEqual(meta.lines_added, 0)
        self.assertEqual(meta.message_hours, [])
        self.assertEqual(meta.user_message_timestamps, [])

    def test_serialization_roundtrip(self):
        meta = SessionMeta(
            session_id="s1", tenant_id="t1", user_id="u1",
            project_path="/p", start_time="2026-01-01T00:00:00",
            duration_minutes=5, user_message_count=2,
            assistant_message_count=3, tool_counts={"Bash": 1},
            languages={"Go": 1}, git_commits=0, git_pushes=0,
            input_tokens=100, output_tokens=200, first_prompt="hi",
        )
        d = asdict(meta)
        restored = SessionMeta(**d)
        self.assertEqual(restored.session_id, "s1")
        self.assertEqual(restored.tool_counts, {"Bash": 1})

    def test_mutable_defaults_independent(self):
        m1 = SessionMeta(
            session_id="a", tenant_id="t", user_id="u",
            project_path="", start_time="", duration_minutes=0,
            user_message_count=0, assistant_message_count=0,
            tool_counts={}, languages={},
            git_commits=0, git_pushes=0,
            input_tokens=0, output_tokens=0, first_prompt="",
        )
        m2 = SessionMeta(
            session_id="b", tenant_id="t", user_id="u",
            project_path="", start_time="", duration_minutes=0,
            user_message_count=0, assistant_message_count=0,
            tool_counts={}, languages={},
            git_commits=0, git_pushes=0,
            input_tokens=0, output_tokens=0, first_prompt="",
        )
        m1.user_response_times.append(5.0)
        self.assertEqual(m2.user_response_times, [])


class TestSessionFacet(unittest.TestCase):
    def test_cc_equivalent_types(self):
        """goal_categories must be Dict[str,int], claude_helpfulness must be str."""
        facet = SessionFacet(
            session_id="s1", underlying_goal="Implement auth",
            goal_categories={"implement_feature": 1, "fix_bug": 1},
            outcome="fully_achieved",
            user_satisfaction_counts={"satisfied": 2},
            claude_helpfulness="very_helpful",
            session_type="multi_task",
        )
        self.assertIsInstance(facet.goal_categories, dict)
        self.assertEqual(facet.goal_categories["implement_feature"], 1)
        self.assertIsInstance(facet.claude_helpfulness, str)
        self.assertEqual(facet.friction_detail, "")
        self.assertEqual(facet.primary_success, "none")
        self.assertEqual(facet.user_instructions_to_claude, [])

    def test_mutable_defaults(self):
        f1 = SessionFacet(
            session_id="a", underlying_goal="g",
            goal_categories={}, outcome="unclear_from_transcript",
            user_satisfaction_counts={}, claude_helpfulness="moderately_helpful",
            session_type="single_task",
        )
        f2 = SessionFacet(
            session_id="b", underlying_goal="g",
            goal_categories={}, outcome="unclear_from_transcript",
            user_satisfaction_counts={}, claude_helpfulness="moderately_helpful",
            session_type="single_task",
        )
        f1.friction_counts["buggy_code"] = 3
        self.assertEqual(f2.friction_counts, {})


class TestAggregatedData(unittest.TestCase):
    def test_creation(self):
        agg = AggregatedData(
            total_sessions=10, total_sessions_scanned=15,
            sessions_with_facets=8,
            date_range={"start": "2026-03-01", "end": "2026-03-31"},
            total_messages=200, total_duration_hours=15.5,
            total_input_tokens=50000, total_output_tokens=80000,
            tool_counts={"Read": 100}, languages={"Python": 50},
            git_commits=20, git_pushes=5,
            projects={"/proj": 10},
            goal_categories={"implement_feature": 5},
            outcomes={"fully_achieved": 7},
            satisfaction={"satisfied": 6},
            helpfulness={"very_helpful": 5},
            session_types={"multi_task": 4},
            friction={"wrong_approach": 3},
            success={"correct_code_edits": 4},
            session_summaries=[],
            total_interruptions=2,
            total_tool_errors=5,
            tool_error_categories={"Command Failed": 3},
            user_response_times=[5.0, 10.0],
            median_response_time=7.5,
            avg_response_time=7.5,
            sessions_using_agent=3,
            sessions_using_mcp=1,
            sessions_using_web_search=0,
            sessions_using_web_fetch=0,
            total_lines_added=500,
            total_lines_removed=200,
            total_files_modified=30,
            days_active=20,
            messages_per_day=10.0,
            message_hours=[9, 10, 14, 15],
            multi_clauding={"overlap_events": 0, "sessions_involved": 0, "user_messages_during": 0},
        )
        self.assertEqual(agg.total_sessions, 10)
        self.assertEqual(agg.median_response_time, 7.5)
        self.assertEqual(agg.multi_clauding["overlap_events"], 0)


class TestInsightsReport(unittest.TestCase):
    def test_enriched_report(self):
        report = InsightsReport(
            tenant_id="t1", user_id="u1",
            report_period="2026-03-01 - 2026-03-31",
            generated_at=datetime(2026, 3, 31, 12, 0, 0),
            total_sessions=10, total_messages=200,
            total_duration_hours=15.5,
        )
        self.assertEqual(report.at_a_glance, {})
        self.assertIsNone(report.interaction_style)
        self.assertIsNone(report.fun_ending)
        self.assertEqual(report.llm_calls, 0)


if __name__ == "__main__":
    unittest.main()
