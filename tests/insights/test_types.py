"""Tests for insights core data types."""

import unittest
from datetime import datetime, timedelta
from opencortex.insights.types import (
    SessionRecord,
    UserActivityWindow,
    SessionFacet,
    InsightsReport,
)


class TestSessionRecord(unittest.TestCase):
    """Test SessionRecord dataclass."""

    def test_session_record_creation(self):
        """Test creating a SessionRecord with all required fields."""
        record = SessionRecord(
            session_id="sess_123",
            tenant_id="tenant_abc",
            user_id="user_xyz",
            project_id="proj_001",
            started_at=datetime(2024, 1, 1, 10, 0, 0),
            ended_at=datetime(2024, 1, 1, 11, 0, 0),
            message_count=25,
            user_message_count=12,
            tool_calls=3,
            memories_created=2,
            memories_referenced=5,
            feedback_given=1,
            session_type="standard",
            outcome="successful",
        )
        self.assertEqual(record.session_id, "sess_123")
        self.assertEqual(record.tenant_id, "tenant_abc")
        self.assertEqual(record.user_id, "user_xyz")
        self.assertEqual(record.project_id, "proj_001")
        self.assertEqual(record.message_count, 25)
        self.assertEqual(record.outcome, "successful")


class TestSessionFacet(unittest.TestCase):
    """Test SessionFacet dataclass."""

    def test_session_facet_creation(self):
        """Test creating a SessionFacet with all fields."""
        facet = SessionFacet(
            session_id="sess_123",
            underlying_goal="Implement user auth",
            brief_summary="User worked on authentication module",
            goal_categories=["backend", "security"],
            outcome="completed",
            user_satisfaction_counts={"satisfied": 1, "neutral": 0, "dissatisfied": 0},
            claude_helpfulness=0.85,
            session_type="standard",
            friction_counts={"api_errors": 2, "design_decisions": 1},
            friction_detail=[
                {"type": "api_errors", "description": "Timeout on OAuth endpoint"}
            ],
            primary_success="Successfully integrated OAuth provider",
        )
        self.assertEqual(facet.session_id, "sess_123")
        self.assertEqual(facet.outcome, "completed")
        self.assertEqual(len(facet.goal_categories), 2)
        self.assertGreater(facet.claude_helpfulness, 0.8)


class TestUserActivityWindow(unittest.TestCase):
    """Test UserActivityWindow dataclass."""

    def test_user_activity_window_aggregation(self):
        """Test creating a UserActivityWindow with aggregated data."""
        start = datetime(2024, 1, 1).date()
        end = datetime(2024, 1, 7).date()
        window = UserActivityWindow(
            start_date=start,
            end_date=end,
            sessions=5,
            total_messages=150,
            total_tokens=45000,
            unique_projects=3,
            tool_usage={"search": 12, "read": 8, "write": 5},
            memory_feedback_score=0.78,
        )
        self.assertEqual(window.sessions, 5)
        self.assertEqual(window.total_messages, 150)
        self.assertEqual(window.total_tokens, 45000)
        self.assertEqual(window.unique_projects, 3)
        self.assertEqual(window.memory_feedback_score, 0.78)


class TestInsightsReport(unittest.TestCase):
    """Test InsightsReport dataclass."""

    def test_insights_report_creation(self):
        """Test creating an InsightsReport."""
        report = InsightsReport(
            tenant_id="tenant_abc",
            user_id="user_xyz",
            report_period="2024-01-01 to 2024-01-31",
            generated_at=datetime(2024, 2, 1, 12, 0, 0),
            total_sessions=20,
            total_messages=500,
            total_duration_hours=12.5,
            session_facets=[],
            project_areas={"backend": 8, "frontend": 7, "devops": 5},
            what_works=[
                "Clear documentation helps implementation",
                "Iterative testing saves debugging time",
            ],
            friction_analysis={"api_errors": 15, "design_decisions": 8},
            suggestions=[
                "Consider using more mocks in testing",
                "Document API contracts earlier",
            ],
            on_the_horizon=["Type safety improvements", "Performance optimization"],
            at_a_glance="Productive month with focus on backend work",
            cache_hits=450,
            llm_calls=50,
        )
        self.assertEqual(report.tenant_id, "tenant_abc")
        self.assertEqual(report.user_id, "user_xyz")
        self.assertEqual(report.total_sessions, 20)
        self.assertEqual(len(report.suggestions), 2)


if __name__ == "__main__":
    unittest.main()
