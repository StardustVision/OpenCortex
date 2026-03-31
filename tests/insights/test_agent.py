"""Tests for InsightsAgent - LLM-powered analysis pipeline."""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from opencortex.insights.types import SessionFacet, InsightsReport
from opencortex.insights.agent import InsightsAgent


class MockLLM:
    """Mock LLM for testing."""

    def generate(self, prompt: str, **kwargs) -> str:
        """Generate mock LLM response based on prompt content."""
        prompt_lower = prompt.lower()

        if "facet_extraction" in prompt or "analyze" in prompt_lower:
            return json.dumps(
                {
                    "underlying_goal": "Fix authentication bug",
                    "brief_summary": "Debugging auth system",
                    "goal_categories": ["debugging", "bug-fix"],
                    "outcome": "fully_achieved",
                    "user_satisfaction_counts": {"satisfied": 2, "neutral": 1},
                    "claude_helpfulness": 0.85,
                    "session_type": "debugging",
                    "friction_counts": {"config_issues": 1},
                    "friction_detail": [
                        {"type": "config_issues", "description": "Token expiry"}
                    ],
                    "primary_success": "Fixed expired token issue",
                }
            )
        elif "chunk_summary" in prompt or "summarize" in prompt_lower:
            return (
                "Session involved debugging authentication. Fixed token expiry issue."
            )
        elif "project_areas" in prompt or "project" in prompt_lower:
            return json.dumps(
                {
                    "areas": ["API Development", "Authentication"],
                    "focus_distribution": {
                        "API Development": 0.6,
                        "Authentication": 0.4,
                    },
                    "cross_cutting_concerns": ["Testing", "Documentation"],
                }
            )
        elif "what_works" in prompt or (
            "what" in prompt_lower and "works" in prompt_lower
        ):
            return json.dumps(
                {
                    "successful_patterns": ["Incremental debugging"],
                    "effective_tools": ["LLM-assisted debugging"],
                    "workflow_strengths": ["Clear problem isolation"],
                    "repeated_successes": ["Breaking down complex issues"],
                }
            )
        elif "friction" in prompt or "friction_analysis" in prompt:
            return json.dumps(
                {
                    "blockers": ["Missing API documentation"],
                    "repeated_issues": ["Configuration complexity"],
                    "inefficient_processes": ["Manual token refresh"],
                    "debugging_friction": ["Unclear error messages"],
                    "tool_friction": ["SDK version conflicts"],
                }
            )
        elif "suggestions" in prompt or "improvement" in prompt_lower:
            return json.dumps(
                {
                    "quick_wins": ["Document token expiry handling"],
                    "process_improvements": ["Automate configuration"],
                    "tool_recommendations": ["Use newer SDK version"],
                    "learning_areas": ["OAuth 2.0 flows"],
                    "automation_opportunities": ["Token refresh"],
                }
            )
        elif "at_a_glance" in prompt or ("glance" in prompt_lower):
            return json.dumps(
                {
                    "headline": "Strong progress on authentication system",
                    "main_activities": [
                        "Fixed token expiry",
                        "Improved error handling",
                    ],
                    "key_challenges": ["Config complexity"],
                    "momentum": "Strong - auth system stabilizing",
                    "next_focus": "API documentation",
                }
            )
        elif "horizon" in prompt or "on_the_horizon" in prompt:
            return json.dumps(
                {
                    "emerging_patterns": ["OAuth 2.0 adoption"],
                    "upcoming_features": ["Multi-tenant support"],
                    "skill_development_areas": ["Advanced auth patterns"],
                    "architectural_evolution": ["Event-driven auth"],
                    "new_problem_domains": ["Mobile auth flows"],
                }
            )
        else:
            return json.dumps(
                {
                    "successful_patterns": ["Incremental debugging"],
                    "quick_wins": ["Document issues"],
                }
            )


class MockCollector:
    """Mock InsightsCollector for testing."""

    def __init__(self):
        self.sessions: List[Dict[str, Any]] = []

    async def collect_user_sessions_async(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
        min_user_messages: int = 1,
    ) -> Dict[str, Any]:
        """Return mock user activity window."""
        return {
            "start_date": start_date,
            "end_date": end_date,
            "sessions": len(self.sessions),
            "total_messages": 50,
            "total_tokens": 5000,
            "unique_projects": 2,
            "tool_usage": {"search": 10, "store": 5},
            "memory_feedback_score": 0.8,
        }


class TestInsightsAgent(unittest.TestCase):
    """Test suite for InsightsAgent."""

    def setUp(self):
        """Set up test fixtures."""
        self.llm = MockLLM()
        self.collector = MockCollector()
        self.agent = InsightsAgent(llm=self.llm, collector=self.collector)
        self.tenant_id = "test-tenant"
        self.user_id = "test-user"
        self.today = date.today()
        self.week_ago = self.today - timedelta(days=7)

    def test_analyze_returns_insights_report(self):
        """Test that analyze() returns a complete InsightsReport."""
        self._setup_mock_sessions()

        report = self.agent.analyze(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            start_date=self.week_ago,
            end_date=self.today,
        )

        self.assertIsInstance(report, InsightsReport)
        self.assertEqual(report.tenant_id, self.tenant_id)
        self.assertEqual(report.user_id, self.user_id)
        self.assertGreater(report.total_sessions, 0)

    def test_analyze_async_returns_insights_report(self):
        """Test that analyze_async() returns a complete InsightsReport."""
        self._setup_mock_sessions()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            report = loop.run_until_complete(
                self.agent.analyze_async(
                    tenant_id=self.tenant_id,
                    user_id=self.user_id,
                    start_date=self.week_ago,
                    end_date=self.today,
                )
            )
        finally:
            loop.close()

        self.assertIsInstance(report, InsightsReport)
        self.assertEqual(report.tenant_id, self.tenant_id)

    def test_extract_session_facets(self):
        """Test _extract_session_facets() extracts facets from sessions."""
        sessions = self._setup_mock_sessions()

        facets = self.agent._extract_session_facets(sessions)

        self.assertIsInstance(facets, list)
        for facet in facets:
            self.assertIsInstance(facet, SessionFacet)
            self.assertIsNotNone(facet.underlying_goal)
            self.assertIsNotNone(facet.session_type)

    def test_filter_warmup_sessions_removes_short_sessions(self):
        """Test _filter_warmup_sessions() removes warmup-only sessions."""
        sessions = [
            self._create_mock_session(message_count=2),
            self._create_mock_session(message_count=10),
        ]
        facets = [
            SessionFacet(
                session_id=s.get("session_id", "s1"),
                underlying_goal="Test",
                brief_summary="Quick test",
                goal_categories=["testing"],
                outcome="partially_achieved",
                user_satisfaction_counts={},
                claude_helpfulness=0.5,
                session_type="unknown",
            )
            for s in sessions
        ]

        filtered = self.agent._filter_warmup_sessions(sessions, facets)

        self.assertLess(len(filtered), len(sessions))

    def test_aggregate_facets_builds_metrics(self):
        """Test _aggregate_facets() builds aggregated metrics."""
        facets = [
            self._create_mock_facet(goal="Fix bug 1"),
            self._create_mock_facet(goal="Fix bug 2"),
        ]

        aggregated = self.agent._aggregate_facets(facets)

        self.assertIn("total_sessions", aggregated)
        self.assertIn("avg_helpfulness", aggregated)
        self.assertGreater(aggregated.get("total_sessions", 0), 0)

    def test_chunk_summarize_handles_long_transcripts(self):
        """Test _chunk_summarize() handles long transcripts."""
        long_transcript = "Message. " * 1000  # Create a long transcript

        summary = self.agent._chunk_summarize(long_transcript)

        self.assertIsInstance(summary, str)
        self.assertGreater(len(summary), 0)
        self.assertLess(len(summary), len(long_transcript))

    def test_generate_project_areas(self):
        """Test _generate_project_areas() generates project analysis."""
        sessions_summary = "Worked on API development and frontend components"

        areas = self.agent._generate_project_areas(sessions_summary)

        self.assertIsInstance(areas, dict)
        self.assertIn("areas", areas)

    def test_generate_what_works(self):
        """Test _generate_what_works() identifies successful patterns."""
        session_data = "Fixed bug quickly using incremental debugging"

        what_works = self.agent._generate_what_works(session_data)

        self.assertIsInstance(what_works, dict)

    def test_generate_friction_analysis(self):
        """Test _generate_friction_analysis() identifies friction points."""
        session_data = "Struggled with configuration complexity"

        friction = self.agent._generate_friction_analysis(session_data)

        self.assertIsInstance(friction, dict)

    def test_generate_suggestions(self):
        """Test _generate_suggestions() creates actionable improvements."""
        findings = {
            "what_works": ["Incremental debugging"],
            "friction": ["Config complexity"],
        }

        suggestions = self.agent._generate_suggestions(findings)

        self.assertIsInstance(suggestions, list)

    def test_generate_at_a_glance(self):
        """Test _generate_at_a_glance() creates summary."""
        insights_data = {
            "total_sessions": 5,
            "total_messages": 50,
            "what_works": ["Pattern 1"],
            "friction": {"issue1": 2},
        }

        summary = self.agent._generate_at_a_glance(insights_data)

        self.assertIsInstance(summary, str)
        self.assertGreater(len(summary), 0)

    # ============ Helper Methods ============

    def _setup_mock_sessions(self) -> List[Dict[str, Any]]:
        """Create mock session data."""
        sessions = [
            self._create_mock_session(
                session_id="s1", message_count=10, transcript="Debugging auth system..."
            ),
            self._create_mock_session(
                session_id="s2",
                message_count=8,
                transcript="Implementing new feature...",
            ),
        ]
        self.collector.sessions = sessions
        return sessions

    def _create_mock_session(
        self,
        session_id: str = "s1",
        message_count: int = 5,
        transcript: str = "Session transcript",
    ) -> Dict[str, Any]:
        """Create a mock session record."""
        return {
            "session_id": session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "started_at": datetime.now() - timedelta(hours=1),
            "ended_at": datetime.now(),
            "message_count": message_count,
            "user_message_count": message_count // 2,
            "transcript": transcript,
        }

    def _create_mock_facet(
        self,
        session_id: str = "s1",
        goal: str = "Fix bug",
    ) -> SessionFacet:
        """Create a mock session facet."""
        return SessionFacet(
            session_id=session_id,
            underlying_goal=goal,
            brief_summary=f"Session: {goal}",
            goal_categories=["debugging"],
            outcome="fully_achieved",
            user_satisfaction_counts={"satisfied": 1},
            claude_helpfulness=0.8,
            session_type="debugging",
        )


if __name__ == "__main__":
    unittest.main()
