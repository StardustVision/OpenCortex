"""ReportManager - Manages insights report storage, retrieval, and HTML rendering."""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from opencortex.insights.types import InsightsReport
from opencortex.utils.uri import CortexURI

logger = logging.getLogger(__name__)


class ReportManager:
    """Manages insights report storage, retrieval, and HTML rendering."""

    def __init__(self, cortex_fs: Any):
        """
        Initialize ReportManager.

        Args:
            cortex_fs: CortexFS instance for file operations
        """
        self._cortex_fs = cortex_fs

    async def save_report(self, report: InsightsReport) -> Dict[str, str]:
        """
        Save JSON and HTML versions of report to CortexFS.

        Args:
            report: InsightsReport instance

        Returns:
            Dictionary with json_uri and html_uri
        """
        date_str = report.generated_at.strftime("%Y-%m-%d")

        json_uri = (
            f"opencortex://{report.tenant_id}/{report.user_id}/"
            f"insights/reports/{date_str}/weekly.json"
        )

        html_uri = (
            f"opencortex://{report.tenant_id}/{report.user_id}/"
            f"insights/reports/{date_str}/weekly.html"
        )

        json_content = self._serialize_report_to_json(report)
        await self._cortex_fs.write(json_uri, json_content, layer="L2")

        html_content = self._render_html(report)
        await self._cortex_fs.write(html_uri, html_content, layer="L2")

        meta_uri = f"opencortex://{report.tenant_id}/{report.user_id}/insights/meta/latest_report.json"
        await self._cortex_fs.write(meta_uri, json_content, layer="L2")

        logger.info(f"Saved report to {json_uri} and {html_uri}")

        return {"json_uri": json_uri, "html_uri": html_uri}

    async def get_latest_report(
        self, tenant_id: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get latest report metadata.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier

        Returns:
            Latest report as dictionary, or None if not found
        """
        meta_uri = (
            f"opencortex://{tenant_id}/{user_id}/insights/meta/latest_report.json"
        )

        try:
            content = await self._cortex_fs.read(meta_uri, layer="L2")
            if content:
                return json.loads(content)
        except Exception as e:
            logger.warning(f"Failed to read latest report metadata: {e}")

        return None

    async def get_report_history(
        self, tenant_id: str, user_id: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Get list of historical reports.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
            limit: Maximum number of reports to return

        Returns:
            List of report metadata dictionaries
        """
        from datetime import timedelta, datetime as dt

        history = []
        search_days = max(limit * 7, 365)

        for i in range(search_days):
            target_date = dt.now() - timedelta(days=i)
            date_str = target_date.strftime("%Y-%m-%d")

            json_uri = (
                f"opencortex://{tenant_id}/{user_id}/"
                f"insights/reports/{date_str}/weekly.json"
            )

            try:
                content = await self._cortex_fs.read(json_uri, layer="L2")
                if content:
                    report_data = json.loads(content)
                    history.append(report_data)
                    if len(history) >= limit:
                        break
            except Exception as e:
                logger.debug(f"No report found for {date_str}: {e}")

        return history

    def _render_html(self, report: InsightsReport) -> str:
        """
        Generate HTML from report data.

        Args:
            report: InsightsReport instance

        Returns:
            HTML string
        """
        html_parts = [
            "<!DOCTYPE html>",
            "<html lang='en'>",
            "<head>",
            "  <meta charset='UTF-8'>",
            "  <meta name='viewport' content='width=device-width, initial-scale=1.0'>",
            "  <title>Insights Report</title>",
            "  <style>",
            "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; "
            "margin: 2rem; background: #f5f5f5; color: #333; }",
            "    .container { max-width: 900px; margin: 0 auto; background: white; "
            "padding: 2rem; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }",
            "    h1 { border-bottom: 3px solid #007bff; padding-bottom: 1rem; color: #007bff; }",
            "    h2 { color: #0056b3; margin-top: 2rem; border-left: 4px solid #007bff; "
            "padding-left: 1rem; }",
            "    .summary { display: grid; grid-template-columns: repeat(auto-fit, "
            "minmax(200px, 1fr)); gap: 1rem; margin: 1.5rem 0; }",
            "    .stat-card { background: #f8f9fa; padding: 1.5rem; border-radius: 6px; "
            "border-left: 4px solid #007bff; }",
            "    .stat-card h3 { margin: 0 0 0.5rem 0; color: #007bff; font-size: 0.9rem; "
            "text-transform: uppercase; }",
            "    .stat-card .value { font-size: 2rem; font-weight: bold; color: #0056b3; }",
            "    ul { line-height: 1.8; }",
            "    li { margin-bottom: 0.5rem; }",
            "    .session-item { background: #f8f9fa; padding: 1rem; "
            "border-radius: 6px; margin: 0.5rem 0; }",
            "    .session-title { font-weight: bold; color: #0056b3; }",
            "    .session-meta { font-size: 0.9rem; color: #666; margin-top: 0.3rem; }",
            "    .report-footer { margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #ddd; "
            "font-size: 0.9rem; color: #666; }",
            "  </style>",
            "</head>",
            "<body>",
            "  <div class='container'>",
        ]

        html_parts.append(f"    <h1>Weekly Insights Report</h1>")
        html_parts.append(f"    <p><strong>Period:</strong> {report.report_period}</p>")
        html_parts.append(
            f"    <p><strong>At a Glance:</strong> {report.at_a_glance}</p>"
        )

        html_parts.append("    <h2>Summary</h2>")
        html_parts.append("    <div class='summary'>")
        html_parts.append(
            f"      <div class='stat-card'><h3>Total Sessions</h3>"
            f"<div class='value'>{report.total_sessions}</div></div>"
        )
        html_parts.append(
            f"      <div class='stat-card'><h3>Total Messages</h3>"
            f"<div class='value'>{report.total_messages}</div></div>"
        )
        html_parts.append(
            f"      <div class='stat-card'><h3>Duration (hours)</h3>"
            f"<div class='value'>{report.total_duration_hours:.1f}</div></div>"
        )
        if report.cache_hits:
            html_parts.append(
                f"      <div class='stat-card'><h3>Cache Hits</h3>"
                f"<div class='value'>{report.cache_hits}</div></div>"
            )
        html_parts.append("    </div>")

        if report.what_works:
            html_parts.append("    <h2>What Works</h2>")
            html_parts.append("    <ul>")
            for item in report.what_works:
                html_parts.append(f"      <li>{self._escape_html(item)}</li>")
            html_parts.append("    </ul>")

        if report.friction_analysis:
            html_parts.append("    <h2>Friction Areas</h2>")
            html_parts.append("    <ul>")
            for area, count in report.friction_analysis.items():
                html_parts.append(
                    f"      <li>{self._escape_html(area)} ({count} occurrences)</li>"
                )
            html_parts.append("    </ul>")

        if report.suggestions:
            html_parts.append("    <h2>Suggestions</h2>")
            html_parts.append("    <ul>")
            for suggestion in report.suggestions:
                html_parts.append(f"      <li>{self._escape_html(suggestion)}</li>")
            html_parts.append("    </ul>")

        if report.on_the_horizon:
            html_parts.append("    <h2>On the Horizon</h2>")
            html_parts.append("    <ul>")
            for item in report.on_the_horizon:
                html_parts.append(f"      <li>{self._escape_html(item)}</li>")
            html_parts.append("    </ul>")

        if report.session_facets:
            html_parts.append("    <h2>Session Details</h2>")
            for facet in report.session_facets:
                html_parts.append(
                    f"      <div class='session-item'>"
                    f"<div class='session-title'>{self._escape_html(facet.brief_summary)}</div>"
                    f"<div class='session-meta'>"
                    f"Goal: {self._escape_html(facet.underlying_goal)} | "
                    f"Type: {facet.session_type} | "
                    f"Outcome: {facet.outcome}"
                    f"</div></div>"
                )

        html_parts.append(
            f"    <div class='report-footer'>"
            f"Generated at {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')}"
            f"</div>"
        )

        html_parts.append("  </div>")
        html_parts.append("</body>")
        html_parts.append("</html>")

        return "\n".join(html_parts)

    def _serialize_report_to_json(self, report: InsightsReport) -> str:
        """
        Serialize report to JSON string.

        Args:
            report: InsightsReport instance

        Returns:
            JSON string
        """
        data = {
            "tenant_id": report.tenant_id,
            "user_id": report.user_id,
            "report_period": report.report_period,
            "generated_at": report.generated_at.isoformat(),
            "total_sessions": report.total_sessions,
            "total_messages": report.total_messages,
            "total_duration_hours": report.total_duration_hours,
            "at_a_glance": report.at_a_glance,
            "cache_hits": report.cache_hits,
            "llm_calls": report.llm_calls,
            "project_areas": report.project_areas,
            "what_works": report.what_works,
            "friction_analysis": report.friction_analysis,
            "suggestions": report.suggestions,
            "on_the_horizon": report.on_the_horizon,
            "session_facets": [
                {
                    "session_id": f.session_id,
                    "underlying_goal": f.underlying_goal,
                    "brief_summary": f.brief_summary,
                    "goal_categories": f.goal_categories,
                    "outcome": f.outcome,
                    "user_satisfaction_counts": f.user_satisfaction_counts,
                    "claude_helpfulness": f.claude_helpfulness,
                    "session_type": f.session_type,
                    "friction_counts": f.friction_counts,
                    "primary_success": f.primary_success,
                }
                for f in report.session_facets
            ],
        }
        return json.dumps(data, indent=2)

    @staticmethod
    def _escape_html(text: str) -> str:
        """
        Escape HTML special characters.

        Args:
            text: Text to escape

        Returns:
            HTML-escaped text
        """
        replacements = {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
        }
        for char, escaped in replacements.items():
            text = text.replace(char, escaped)
        return text
