# SPDX-License-Identifier: Apache-2.0
"""
FastAPI routes for insights API endpoints.

Provides endpoints for:
- On-demand insight generation
- Latest report retrieval
- Report history
- Periodic scheduling
"""

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from opencortex.http.request_context import get_effective_identity

logger = logging.getLogger(__name__)


# =========================================================================
# Request/Response Models
# =========================================================================


class GenerateInsightsRequest(BaseModel):
    """Request to generate insights on demand."""

    days: int = Field(default=7, ge=1, le=90, description="Number of days to analyze")


class GenerateInsightsResponse(BaseModel):
    """Response from generate insights endpoint."""

    report_uri: str = Field(..., description="URI of the generated report")
    summary: str = Field(..., description="At-a-glance summary of the report")
    generated_at: datetime = Field(..., description="Report generation timestamp")


class ReportMetadata(BaseModel):
    """Metadata for a report."""

    report_uri: str = Field(..., description="URI of the report")
    generated_at: datetime = Field(..., description="When the report was generated")
    period_start: date = Field(..., description="Analysis period start date")
    period_end: date = Field(..., description="Analysis period end date")
    total_sessions: int = Field(..., description="Number of sessions analyzed")
    total_messages: int = Field(..., description="Total messages in period")


class LatestReportResponse(BaseModel):
    """Response containing latest report metadata."""

    report: Optional[ReportMetadata] = Field(None, description="Latest report metadata")
    message: str = Field(..., description="Status message")


class ReportHistoryResponse(BaseModel):
    """Response containing list of historical reports."""

    reports: list[ReportMetadata] = Field(..., description="List of historical reports")
    total: int = Field(..., description="Total number of reports available")


class ScheduleInsightsRequest(BaseModel):
    """Request to schedule periodic insights."""

    cron_expression: str = Field(
        default="0 0 * * 0",
        description="Cron expression for scheduling (default: weekly Sunday 00:00)",
    )
    timezone: str = Field(default="UTC", description="Timezone for cron schedule")


class ScheduleInsightsResponse(BaseModel):
    """Response from schedule insights endpoint."""

    job_id: str = Field(..., description="Unique identifier for the scheduled job")
    cron_expression: str = Field(..., description="Cron expression for the job")
    timezone: str = Field(..., description="Timezone for the schedule")
    next_run: Optional[datetime] = Field(None, description="Timestamp of next run")
    message: str = Field(..., description="Status message")


# =========================================================================
# Route Handlers
# =========================================================================


def create_insights_router(
    scheduler: Any,
    report_manager: Any,
    orchestrator: Any,
) -> APIRouter:
    """
    Create FastAPI router for insights endpoints.

    Args:
        scheduler: InsightsScheduler instance
        report_manager: ReportManager instance
        orchestrator: MemoryOrchestrator instance

    Returns:
        FastAPI APIRouter with all insights endpoints
    """
    router = APIRouter(prefix="/api/v1/insights", tags=["insights"])

    # =====================================================================
    # POST /api/v1/insights/generate
    # =====================================================================

    @router.post("/generate", response_model=GenerateInsightsResponse)
    async def generate_insights(
        days: int = Query(default=7, ge=1, le=90, description="Days to analyze"),
    ) -> Dict[str, Any]:
        """
        Generate insights on demand for the current user.

        Query Parameters:
        - days: Number of days to analyze (default: 7, max: 90)

        Returns:
        - report_uri: URI of the generated report
        - summary: At-a-glance summary
        - generated_at: Timestamp

        Errors:
        - 401: Unauthorized (no valid JWT)
        - 500: Generation failed
        """
        try:
            # Get current user from JWT context
            tid, uid = get_effective_identity()
            if not tid or not uid:
                raise HTTPException(status_code=401, detail="Unauthorized")

            # Calculate date range
            end_date = date.today()
            start_date = end_date - timedelta(days=days)

            logger.info(
                f"Generating insights for {tid}/{uid} from {start_date} to {end_date}"
            )

            # Generate insights via scheduler (which has agent and report_manager)
            await scheduler.generate_now(
                tenant_id=tid,
                user_id=uid,
                start_date=start_date,
                end_date=end_date,
            )

            # Get the report metadata
            report = await report_manager.get_latest_report(tid, uid)
            if not report:
                raise HTTPException(
                    status_code=500,
                    detail="Report generated but could not retrieve metadata",
                )

            return {
                "report_uri": f"opencortex://{tid}/{uid}/insights/reports/{end_date.isoformat()}/weekly.json",
                "summary": report.get("at_a_glance", "Report generated successfully"),
                "generated_at": datetime.fromisoformat(report.get("generated_at", "")),
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error generating insights: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")

    # =====================================================================
    # GET /api/v1/insights/latest
    # =====================================================================

    @router.get("/latest", response_model=LatestReportResponse)
    async def get_latest_insights() -> Dict[str, Any]:
        """
        Get latest insights report metadata for the current user.

        Returns:
        - report: ReportMetadata or null if no reports exist
        - message: Status message

        Errors:
        - 401: Unauthorized
        - 500: Retrieval failed
        """
        try:
            tid, uid = get_effective_identity()
            if not tid or not uid:
                raise HTTPException(status_code=401, detail="Unauthorized")

            report = await report_manager.get_latest_report(tid, uid)
            if not report:
                return {
                    "report": None,
                    "message": "No reports generated yet",
                }

            return {
                "report": {
                    "report_uri": report.get("json_uri", ""),
                    "generated_at": datetime.fromisoformat(
                        report.get("generated_at", "")
                    ),
                    "period_start": date.fromisoformat(
                        report.get("report_period", "").split(" - ")[0]
                    )
                    if " - " in report.get("report_period", "")
                    else date.today(),
                    "period_end": date.fromisoformat(
                        report.get("report_period", "").split(" - ")[1]
                    )
                    if " - " in report.get("report_period", "")
                    else date.today(),
                    "total_sessions": report.get("total_sessions", 0),
                    "total_messages": report.get("total_messages", 0),
                },
                "message": "Latest report retrieved",
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error retrieving latest report: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Retrieval failed: {str(e)}")

    # =====================================================================
    # GET /api/v1/insights/history
    # =====================================================================

    @router.get("/history", response_model=ReportHistoryResponse)
    async def get_insights_history(
        limit: int = Query(
            default=10, ge=1, le=100, description="Max reports to return"
        ),
    ) -> Dict[str, Any]:
        """
        Get report history for the current user.

        Query Parameters:
        - limit: Maximum number of reports to return (default: 10, max: 100)

        Returns:
        - reports: List of ReportMetadata
        - total: Total number of reports available

        Errors:
        - 401: Unauthorized
        - 500: Retrieval failed
        """
        try:
            tid, uid = get_effective_identity()
            if not tid or not uid:
                raise HTTPException(status_code=401, detail="Unauthorized")

            reports = await report_manager.get_report_history(
                tenant_id=tid, user_id=uid, limit=limit
            )

            # Convert reports to response format
            report_list = []
            for report in reports:
                try:
                    period_str = report.get("report_period", " - ")
                    parts = period_str.split(" - ")
                    period_start = (
                        date.fromisoformat(parts[0]) if len(parts) > 0 else date.today()
                    )
                    period_end = (
                        date.fromisoformat(parts[1]) if len(parts) > 1 else date.today()
                    )
                except (ValueError, IndexError):
                    period_start = date.today()
                    period_end = date.today()

                report_list.append(
                    {
                        "report_uri": report.get("json_uri", ""),
                        "generated_at": datetime.fromisoformat(
                            report.get("generated_at", "")
                        ),
                        "period_start": period_start,
                        "period_end": period_end,
                        "total_sessions": report.get("total_sessions", 0),
                        "total_messages": report.get("total_messages", 0),
                    }
                )

            return {
                "reports": report_list,
                "total": len(report_list),
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error retrieving report history: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Retrieval failed: {str(e)}")

    # =====================================================================
    # POST /api/v1/insights/schedule
    # =====================================================================

    @router.post("/schedule", response_model=ScheduleInsightsResponse)
    async def schedule_insights(req: ScheduleInsightsRequest) -> Dict[str, Any]:
        """
        Schedule periodic insights generation for the current user.

        Request Body:
        - cron_expression: Cron expression for schedule (default: "0 0 * * 0")
        - timezone: Timezone for schedule (default: "UTC")

        Returns:
        - job_id: Unique identifier for the scheduled job
        - cron_expression: The cron expression
        - timezone: The timezone
        - next_run: Estimated timestamp of next run
        - message: Status message

        Errors:
        - 401: Unauthorized
        - 400: Invalid cron expression
        - 500: Scheduling failed
        """
        try:
            tid, uid = get_effective_identity()
            if not tid or not uid:
                raise HTTPException(status_code=401, detail="Unauthorized")

            # Validate cron expression (basic check)
            parts = req.cron_expression.strip().split()
            if len(parts) != 5:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid cron expression: must have 5 parts (minute hour day month weekday)",
                )

            logger.info(
                f"Scheduling insights for {tid}/{uid} with cron: {req.cron_expression}"
            )

            # Schedule the job
            job_id = scheduler.schedule_user_insights(
                tenant_id=tid,
                user_id=uid,
                cron_expression=req.cron_expression,
            )

            return {
                "job_id": job_id,
                "cron_expression": req.cron_expression,
                "timezone": req.timezone,
                "next_run": None,  # APScheduler doesn't easily expose next_run
                "message": f"Insights scheduled with job ID: {job_id}",
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error scheduling insights: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Scheduling failed: {str(e)}")

    return router
