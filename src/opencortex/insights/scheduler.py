"""InsightsScheduler - Periodic task scheduling for weekly insights generation."""

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from opencortex.insights.agent import InsightsAgent
from opencortex.insights.report import ReportManager

logger = logging.getLogger(__name__)

DEFAULT_CRON_EXPRESSION = "0 0 * * 0"


class InsightsScheduler:
    """Periodic task scheduling using APScheduler for weekly insights generation."""

    def __init__(self, agent: InsightsAgent, report_manager: ReportManager):
        """
        Initialize InsightsScheduler.

        Args:
            agent: InsightsAgent instance for analysis
            report_manager: ReportManager instance for storing reports
        """
        self._agent = agent
        self._report_manager = report_manager
        self._scheduler = BackgroundScheduler()

    def start(self) -> None:
        """Start the scheduler."""
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("InsightsScheduler started")

    def stop(self) -> None:
        """Stop the scheduler."""
        if self._scheduler.running:
            self._scheduler.shutdown()
            logger.info("InsightsScheduler stopped")

    def schedule_user_insights(
        self,
        tenant_id: str,
        user_id: str,
        cron_expression: Optional[str] = None,
    ) -> str:
        """
        Schedule periodic insights for a user.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
            cron_expression: Cron expression for scheduling (default: weekly Sunday 00:00)

        Returns:
            Job ID for the scheduled task
        """
        job_id = f"insights_{tenant_id}_{user_id}"
        cron = cron_expression or DEFAULT_CRON_EXPRESSION

        self._scheduler.add_job(
            self._generate_insights_job,
            trigger=CronTrigger.from_crontab(cron),
            args=(tenant_id, user_id),
            id=job_id,
            replace_existing=True,
        )

        logger.info(f"Scheduled insights job {job_id} with cron expression: {cron}")
        return job_id

    def unschedule_user_insights(self, tenant_id: str, user_id: str) -> None:
        """
        Remove scheduled job for a user.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
        """
        job_id = f"insights_{tenant_id}_{user_id}"

        try:
            self._scheduler.remove_job(job_id)
            logger.info(f"Unscheduled insights job {job_id}")
        except Exception as e:
            logger.warning(f"Failed to unschedule job {job_id}: {e}")

    async def _generate_insights_job(self, tenant_id: str, user_id: str) -> None:
        """
        Background job to generate insights for a user.

        Generates insights for the past 7 days.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
        """
        try:
            end_date = date.today()
            start_date = end_date - timedelta(days=7)

            await self.generate_now(
                tenant_id=tenant_id,
                user_id=user_id,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception as e:
            logger.error(
                f"Error generating insights for {tenant_id}/{user_id}: {e}",
                exc_info=True,
            )

    async def generate_now(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> None:
        """
        Generate insights on demand.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
            start_date: Analysis start date
            end_date: Analysis end date
        """
        try:
            report = await self._agent.analyze_async(
                tenant_id=tenant_id,
                user_id=user_id,
                start_date=start_date,
                end_date=end_date,
            )

            await self._report_manager.save_report(report)

            logger.info(
                f"Generated and saved insights for {tenant_id}/{user_id} "
                f"({start_date} to {end_date})"
            )
        except Exception as e:
            logger.error(
                f"Error generating insights for {tenant_id}/{user_id}: {e}",
                exc_info=True,
            )
