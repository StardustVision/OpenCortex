"""Insights module for analyzing user behavior and session patterns."""

from opencortex.insights.agent import InsightsAgent
from opencortex.insights.collector import InsightsCollector
from opencortex.insights.scheduler import InsightsScheduler
from opencortex.insights.report import ReportManager
from opencortex.insights.types import (
    SessionRecord,
    SessionFacet,
    UserActivityWindow,
    InsightsReport,
)

__all__ = [
    "InsightsAgent",
    "InsightsCollector",
    "InsightsScheduler",
    "ReportManager",
    "SessionRecord",
    "SessionFacet",
    "UserActivityWindow",
    "InsightsReport",
]
