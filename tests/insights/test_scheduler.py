"""Tests for InsightsScheduler - periodic task scheduling for insights generation."""

import asyncio
import unittest
from datetime import datetime, date, timedelta
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

from opencortex.insights.types import InsightsReport
from opencortex.insights.scheduler import InsightsScheduler


class MockInsightsAgent:
    """Mock InsightsAgent for testing."""

    def __init__(self):
        """Initialize mock agent."""
        self.analyze_async = AsyncMock(
            return_value=InsightsReport(
                tenant_id="test",
                user_id="test",
                report_period="test",
                generated_at=datetime.now(),
                total_sessions=1,
                total_messages=10,
                total_duration_hours=1.0,
            )
        )


class MockReportManager:
    """Mock ReportManager for testing."""

    def __init__(self):
        """Initialize mock report manager."""
        self.save_report = AsyncMock()
        self.get_latest_report = AsyncMock()
        self.get_report_history = AsyncMock()

    async def save_report(self, report: InsightsReport) -> Dict[str, str]:
        """Mock save_report method."""
        return {
            "json_uri": f"opencortex://{report.tenant_id}/{report.user_id}/insights/reports/2024-01-07/weekly.json",
            "html_uri": f"opencortex://{report.tenant_id}/{report.user_id}/insights/reports/2024-01-07/weekly.html",
        }


class TestInsightsScheduler(unittest.TestCase):
    """Test suite for InsightsScheduler."""

    def setUp(self):
        """Set up test fixtures."""
        self.agent = MockInsightsAgent()
        self.report_manager = MockReportManager()
        self.scheduler = InsightsScheduler(self.agent, self.report_manager)

    def test_init_creates_scheduler_with_agent_and_manager(self):
        """Test that __init__ stores agent and report manager."""
        self.assertEqual(self.scheduler._agent, self.agent)
        self.assertEqual(self.scheduler._report_manager, self.report_manager)
        self.assertIsNotNone(self.scheduler._scheduler)

    def test_start_starts_the_scheduler(self):
        """Test that start() starts the scheduler."""
        self.scheduler.start()
        self.assertTrue(self.scheduler._scheduler.running)
        self.scheduler.stop()

    def test_stop_stops_the_scheduler(self):
        """Test that stop() stops the scheduler."""
        self.scheduler.start()
        self.assertTrue(self.scheduler._scheduler.running)
        self.scheduler.stop()
        self.assertFalse(self.scheduler._scheduler.running)

    def test_schedule_user_insights_creates_job_with_cron_expression(self):
        """Test that schedule_user_insights creates job with cron expression."""
        self.scheduler.start()

        job_id = self.scheduler.schedule_user_insights(
            tenant_id="test-tenant",
            user_id="user-123",
            cron_expression="0 0 * * 0",  # Weekly on Sunday
        )

        # Job ID should be formatted correctly
        self.assertEqual(job_id, "insights_test-tenant_user-123")

        # Verify job was added
        job = self.scheduler._scheduler.get_job(job_id)
        self.assertIsNotNone(job)
        self.scheduler.stop()

    def test_schedule_user_insights_uses_default_cron_if_not_provided(self):
        """Test that schedule_user_insights uses default weekly cron expression."""
        self.scheduler.start()

        job_id = self.scheduler.schedule_user_insights(
            tenant_id="test-tenant", user_id="user-123"
        )

        job = self.scheduler._scheduler.get_job(job_id)
        self.assertIsNotNone(job)
        day_of_week_field = job.trigger.fields[4]
        hour_field = job.trigger.fields[5]
        minute_field = job.trigger.fields[6]
        self.assertEqual(day_of_week_field.name, "day_of_week")
        self.assertEqual(str(day_of_week_field), "0")
        self.assertEqual(hour_field.name, "hour")
        self.assertEqual(str(hour_field), "0")
        self.scheduler.stop()

    def test_unschedule_user_insights_removes_job(self):
        """Test that unschedule_user_insights removes scheduled job."""
        self.scheduler.start()

        job_id = self.scheduler.schedule_user_insights(
            tenant_id="test-tenant", user_id="user-123"
        )

        # Verify job exists
        job = self.scheduler._scheduler.get_job(job_id)
        self.assertIsNotNone(job)

        # Unschedule
        self.scheduler.unschedule_user_insights(
            tenant_id="test-tenant", user_id="user-123"
        )

        # Verify job is removed
        job = self.scheduler._scheduler.get_job(job_id)
        self.assertIsNone(job)
        self.scheduler.stop()

    def test_generate_now_generates_insights_immediately(self):
        """Test that generate_now() generates insights on demand."""
        asyncio.run(
            self.scheduler.generate_now(
                tenant_id="test-tenant",
                user_id="user-123",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 7),
            )
        )

        self.agent.analyze_async.assert_called_once_with(
            tenant_id="test-tenant",
            user_id="user-123",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 7),
        )

        self.report_manager.save_report.assert_called_once()

    def test_generate_insights_job_background_execution(self):
        """Test that _generate_insights_job executes background insights generation."""
        self.scheduler.start()

        asyncio.run(
            self.scheduler._generate_insights_job(
                tenant_id="test-tenant",
                user_id="user-123",
            )
        )

        self.agent.analyze_async.assert_called_once()
        call_kwargs = self.agent.analyze_async.call_args.kwargs
        self.assertEqual(call_kwargs["tenant_id"], "test-tenant")
        self.assertEqual(call_kwargs["user_id"], "user-123")

        self.report_manager.save_report.assert_called_once()
        self.scheduler.stop()

    def test_generate_insights_job_calculates_correct_date_range(self):
        """Test that _generate_insights_job uses correct date range (last 7 days)."""
        asyncio.run(
            self.scheduler._generate_insights_job(
                tenant_id="test-tenant",
                user_id="user-123",
            )
        )

        call_kwargs = self.agent.analyze_async.call_args.kwargs
        start_date = call_kwargs["start_date"]
        end_date = call_kwargs["end_date"]

        today = date.today()
        self.assertEqual(end_date, today)

        expected_start = today - timedelta(days=7)
        self.assertEqual(start_date, expected_start)

    def test_schedule_user_insights_with_custom_cron(self):
        """Test scheduling with custom cron expression."""
        self.scheduler.start()

        job_id = self.scheduler.schedule_user_insights(
            tenant_id="test-tenant",
            user_id="user-123",
            cron_expression="0 9 * * 1",
        )

        job = self.scheduler._scheduler.get_job(job_id)
        self.assertIsNotNone(job)
        day_of_week_field = job.trigger.fields[4]
        hour_field = job.trigger.fields[5]
        self.assertEqual(day_of_week_field.name, "day_of_week")
        self.assertEqual(str(day_of_week_field), "1")
        self.assertEqual(hour_field.name, "hour")
        self.assertEqual(str(hour_field), "9")
        self.scheduler.stop()

    def test_multiple_users_can_be_scheduled(self):
        """Test that multiple users can have separate scheduled jobs."""
        self.scheduler.start()

        job_id_1 = self.scheduler.schedule_user_insights(
            tenant_id="test-tenant", user_id="user-1"
        )
        job_id_2 = self.scheduler.schedule_user_insights(
            tenant_id="test-tenant", user_id="user-2"
        )

        self.assertNotEqual(job_id_1, job_id_2)

        job_1 = self.scheduler._scheduler.get_job(job_id_1)
        job_2 = self.scheduler._scheduler.get_job(job_id_2)

        self.assertIsNotNone(job_1)
        self.assertIsNotNone(job_2)
        self.scheduler.stop()

    def test_multi_tenant_job_isolation(self):
        """Test that jobs are isolated per tenant and user."""
        self.scheduler.start()

        job_id_1 = self.scheduler.schedule_user_insights(
            tenant_id="tenant-1", user_id="user-123"
        )
        job_id_2 = self.scheduler.schedule_user_insights(
            tenant_id="tenant-2", user_id="user-123"
        )

        # Job IDs should be different (different tenants)
        self.assertNotEqual(job_id_1, job_id_2)
        self.assertEqual(job_id_1, "insights_tenant-1_user-123")
        self.assertEqual(job_id_2, "insights_tenant-2_user-123")

        self.scheduler.stop()

    def test_unscheduling_nonexistent_job_doesnt_raise_error(self):
        """Test that unscheduling a nonexistent job doesn't raise an error."""
        self.scheduler.start()

        # Should not raise
        self.scheduler.unschedule_user_insights(
            tenant_id="nonexistent", user_id="user-999"
        )

        self.scheduler.stop()

    def test_scheduler_start_and_stop_multiple_times(self):
        """Test that scheduler can be started and stopped multiple times."""
        self.scheduler.start()
        self.assertTrue(self.scheduler._scheduler.running)

        self.scheduler.stop()
        self.assertFalse(self.scheduler._scheduler.running)

        self.scheduler.start()
        self.assertTrue(self.scheduler._scheduler.running)

        self.scheduler.stop()
        self.assertFalse(self.scheduler._scheduler.running)

    def test_generate_now_uses_provided_date_range(self):
        """Test that generate_now uses the provided date range."""
        start_date = date(2024, 1, 1)
        end_date = date(2024, 1, 31)

        asyncio.run(
            self.scheduler.generate_now(
                tenant_id="test-tenant",
                user_id="user-123",
                start_date=start_date,
                end_date=end_date,
            )
        )

        call_kwargs = self.agent.analyze_async.call_args.kwargs
        self.assertEqual(call_kwargs["start_date"], start_date)
        self.assertEqual(call_kwargs["end_date"], end_date)


if __name__ == "__main__":
    unittest.main()
