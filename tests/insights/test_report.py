"""Tests for ReportManager."""

import unittest
import json
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Any
from unittest.mock import AsyncMock, MagicMock, patch

from opencortex.insights.types import InsightsReport, SessionFacet
from opencortex.insights.report import ReportManager


class MockCortexFS:
    """Mock CortexFS for testing."""

    def __init__(self):
        self.stored_files: Dict[str, str] = {}

    async def write(self, uri: str, content: str, layer: str = "L2") -> None:
        """Mock write method."""
        key = f"{uri}#{layer}"
        self.stored_files[key] = content

    async def read(self, uri: str, layer: str = "L2") -> Optional[str]:
        """Mock read method."""
        key = f"{uri}#{layer}"
        return self.stored_files.get(key)

    async def exists(self, uri: str) -> bool:
        """Mock exists method."""
        return any(k.startswith(f"{uri}#") for k in self.stored_files.keys())


class TestReportManager(unittest.TestCase):
    """Test cases for ReportManager."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_cortex_fs = MockCortexFS()
        self.report_manager = ReportManager(cortex_fs=self.mock_cortex_fs)

        # Sample insights report
        self.sample_facet = SessionFacet(
            session_id="s1",
            underlying_goal="Fix authentication bug",
            brief_summary="Debugged and fixed JWT token validation issue",
            goal_categories=["debugging", "security"],
            outcome="fully_achieved",
            user_satisfaction_counts={"satisfied": 1},
            claude_helpfulness=0.95,
            session_type="debugging",
            friction_counts={},
            friction_detail=[],
        )

        self.sample_report = InsightsReport(
            tenant_id="tenant1",
            user_id="user1",
            report_period="2025-03-24 to 2025-03-30",
            generated_at=datetime(2025, 3, 30, 10, 0, 0),
            total_sessions=5,
            total_messages=127,
            total_duration_hours=12.5,
            session_facets=[self.sample_facet],
            project_areas={"auth": 3, "api": 2},
            what_works=["Quick debugging with memory context", "Iterative refinement"],
            friction_analysis={"UI complexity": 2},
            suggestions=["Consider caching tokens", "Add rate limiting"],
            on_the_horizon=["OAuth2 integration", "Multi-tenant support"],
            at_a_glance="Productive week with focus on security hardening",
            cache_hits=45,
            llm_calls=23,
        )

    def test_save_report_creates_json_and_html(self):
        """Test that save_report creates both JSON and HTML files."""
        # This test would normally be async, but we'll use a helper
        result = self._run_async(self.report_manager.save_report(self.sample_report))

        # Verify JSON was written
        json_key = (
            f"opencortex://tenant1/user1/insights/reports/2025-03-30/weekly.json#L2"
        )
        assert json_key in self.mock_cortex_fs.stored_files

        # Verify HTML was written
        html_key = (
            f"opencortex://tenant1/user1/insights/reports/2025-03-30/weekly.html#L2"
        )
        assert html_key in self.mock_cortex_fs.stored_files

    def test_get_latest_report_returns_metadata(self):
        """Test that get_latest_report returns report metadata."""
        # First save a report
        self._run_async(self.report_manager.save_report(self.sample_report))

        # Then get it back
        result = self._run_async(
            self.report_manager.get_latest_report(
                tenant_id="tenant1",
                user_id="user1",
            )
        )

        assert result is not None
        assert result["total_sessions"] == 5
        assert result["report_period"] == "2025-03-24 to 2025-03-30"

    def test_get_report_history_lists_reports(self):
        """Test that get_report_history returns list of reports."""
        today = datetime.now()
        yesterday = today - timedelta(days=1)

        report1 = self.sample_report
        report2 = InsightsReport(
            tenant_id="tenant1",
            user_id="user1",
            report_period=f"{yesterday.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}",
            generated_at=yesterday,
            total_sessions=4,
            total_messages=100,
            total_duration_hours=10.0,
            session_facets=[],
            project_areas={},
            what_works=[],
            friction_analysis={},
            suggestions=[],
            on_the_horizon=[],
        )

        self._run_async(self.report_manager.save_report(report1))
        self._run_async(self.report_manager.save_report(report2))

        history = self._run_async(
            self.report_manager.get_report_history(
                tenant_id="tenant1",
                user_id="user1",
                limit=10,
            )
        )

        assert len(history) >= 1

    def test_html_rendering_includes_key_sections(self):
        """Test that HTML rendering includes all key sections."""
        self._run_async(self.report_manager.save_report(self.sample_report))

        html_key = (
            f"opencortex://tenant1/user1/insights/reports/2025-03-30/weekly.html#L2"
        )
        html_content = self.mock_cortex_fs.stored_files[html_key]

        # Check for key HTML sections
        assert "<!DOCTYPE html>" in html_content
        assert "Insights Report" in html_content or "weekly" in html_content.lower()
        assert str(self.sample_report.total_sessions) in html_content
        assert "What Works" in html_content or "what_works" in html_content.lower()

    def test_html_rendering_handles_empty_lists(self):
        """Test HTML rendering with minimal data."""
        minimal_report = InsightsReport(
            tenant_id="tenant1",
            user_id="user1",
            report_period="2025-03-24 to 2025-03-30",
            generated_at=datetime(2025, 3, 30, 10, 0, 0),
            total_sessions=0,
            total_messages=0,
            total_duration_hours=0.0,
        )

        self._run_async(self.report_manager.save_report(minimal_report))

        html_key = (
            f"opencortex://tenant1/user1/insights/reports/2025-03-30/weekly.html#L2"
        )
        html_content = self.mock_cortex_fs.stored_files[html_key]

        # Should render without errors
        assert "<!DOCTYPE html>" in html_content

    def test_json_content_matches_report_data(self):
        """Test that JSON content preserves all report data."""
        self._run_async(self.report_manager.save_report(self.sample_report))

        json_key = (
            f"opencortex://tenant1/user1/insights/reports/2025-03-30/weekly.json#L2"
        )
        json_str = self.mock_cortex_fs.stored_files[json_key]
        data = json.loads(json_str)

        assert data["tenant_id"] == "tenant1"
        assert data["user_id"] == "user1"
        assert data["total_sessions"] == 5
        assert data["total_messages"] == 127
        assert len(data["session_facets"]) == 1

    def _run_async(self, coro):
        """Helper to run async functions in tests."""
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


class TestEnrichedSerialization(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fs = AsyncMock()
        self.manager = ReportManager(self.fs)

    async def test_serialize_includes_enriched_fields(self):
        report = InsightsReport(
            tenant_id="t1", user_id="u1",
            report_period="2026-03-01 - 2026-03-31",
            generated_at=datetime(2026, 3, 31, 12, 0),
            total_sessions=5, total_messages=50,
            total_duration_hours=3.0,
            at_a_glance={
                "whats_working": "Good patterns",
                "whats_hindering": "Some friction",
                "quick_wins": "Try feedback",
                "ambitious_workflows": "Autonomous testing",
            },
            interaction_style={"narrative": "Fast coder", "key_pattern": "Iterative"},
            fun_ending={"headline": "It worked!", "detail": "Session 3"},
            aggregated={"tool_counts": {"Read": 10}, "languages": {"Python": 5}},
        )
        json_str = self.manager._serialize_report_to_json(report)
        data = json.loads(json_str)
        self.assertIsInstance(data["at_a_glance"], dict)
        self.assertEqual(data["at_a_glance"]["whats_working"], "Good patterns")
        self.assertEqual(data["interaction_style"]["narrative"], "Fast coder")
        self.assertEqual(data["fun_ending"]["headline"], "It worked!")
        self.assertIn("tool_counts", data["aggregated"])

    async def test_serialize_backward_compat(self):
        report = InsightsReport(
            tenant_id="t1", user_id="u1",
            report_period="2026-03-01 - 2026-03-31",
            generated_at=datetime(2026, 3, 31),
            total_sessions=0, total_messages=0,
            total_duration_hours=0,
        )
        json_str = self.manager._serialize_report_to_json(report)
        data = json.loads(json_str)
        self.assertEqual(data["at_a_glance"], {})
        self.assertIsNone(data["interaction_style"])


if __name__ == "__main__":
    unittest.main()
