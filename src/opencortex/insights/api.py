# SPDX-License-Identifier: Apache-2.0
"""
FastAPI routes for insights API endpoints.

Provides endpoints for:
- On-demand insight generation
- Latest report retrieval
- Report history
"""

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from opencortex.http.request_context import get_effective_identity

logger = logging.getLogger(__name__)
def _parse_report_period(period_str: str) -> Tuple[date, date]:
    """Parse stored report_period strings with either separator."""
    fallback = date.today()
    for sep in (" - ", " to "):
        if sep in period_str:
            parts = period_str.split(sep)
            if len(parts) >= 2:
                try:
                    return date.fromisoformat(parts[0]), date.fromisoformat(parts[1])
                except ValueError:
                    break
    return fallback, fallback


# =========================================================================
# Request/Response Models
# =========================================================================


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


class ReportContentResponse(BaseModel):
    """Full report content from CortexFS."""

    pass  # Returns raw JSON, no fixed schema needed


# =========================================================================
# Route Handlers
# =========================================================================


def create_insights_router(
    agent: Any,
    report_manager: Any,
    orchestrator: Any,
) -> APIRouter:
    """
    Create FastAPI router for insights endpoints.

    Args:
        agent: InsightsAgent instance
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
            tid, uid = get_effective_identity()
            if not tid or not uid:
                raise HTTPException(status_code=401, detail="Unauthorized")

            end_date = date.today()
            start_date = end_date - timedelta(days=days)

            logger.info(
                f"Generating insights for {tid}/{uid} from {start_date} to {end_date}"
            )

            report = await agent.analyze_async(
                tenant_id=tid,
                user_id=uid,
                start_date=start_date,
                end_date=end_date,
            )
            await report_manager.save_report(report)

            meta = await report_manager.get_latest_report(tid, uid)
            if not meta:
                raise HTTPException(
                    status_code=500,
                    detail="Report generated but could not retrieve metadata",
                )

            # at_a_glance may be a Dict[str, str] or a string
            glance = meta.get("at_a_glance", "Report generated successfully")
            if isinstance(glance, dict):
                summary = glance.get("headline") or "; ".join(
                    v for v in glance.values() if v
                ) or "Report generated successfully"
            else:
                summary = str(glance) if glance else "Report generated successfully"

            return {
                "report_uri": meta.get("json_uri", f"opencortex://{tid}/{uid}/insights/reports/{end_date.isoformat()}/weekly.json"),
                "summary": summary,
                "generated_at": datetime.fromisoformat(meta.get("generated_at", "")),
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

            period_start, period_end = _parse_report_period(
                report.get("report_period", "")
            )

            return {
                "report": {
                    "report_uri": report.get("json_uri", ""),
                    "generated_at": datetime.fromisoformat(
                        report.get("generated_at", "")
                    ),
                    "period_start": period_start,
                    "period_end": period_end,
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

            report_list = []
            for report in reports:
                period_start, period_end = _parse_report_period(
                    report.get("report_period", "")
                )

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
    # GET /api/v1/insights/report
    # =====================================================================

    @router.get("/report")
    async def get_report_content(
        report_uri: str = Query(..., description="Report URI from /history or /latest"),
    ) -> Dict[str, Any]:
        """
        Get full report content by URI.

        Query Parameters:
        - report_uri: opencortex:// URI of the report

        Returns:
        - Full InsightsReport JSON

        Errors:
        - 401: Unauthorized
        - 403: URI does not belong to requesting user
        - 404: Report not found
        """
        try:
            tid, uid = get_effective_identity()
            if not tid or not uid:
                raise HTTPException(status_code=401, detail="Unauthorized")

            # Security: verify URI belongs to requesting user
            expected_prefix = f"opencortex://{tid}/{uid}/"
            if not report_uri.startswith(expected_prefix):
                raise HTTPException(
                    status_code=403,
                    detail="Access denied: report does not belong to requesting user",
                )

            content = await report_manager._cortex_fs.read(report_uri)
            if not content:
                raise HTTPException(status_code=404, detail="Report not found")

            return json.loads(content)

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error reading report: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Read failed: {str(e)}")

    return router
