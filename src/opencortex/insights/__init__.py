"""Insights module for analyzing user behavior and session patterns."""

from opencortex.insights.types import (
    SessionMeta,
    SessionFacet,
    AggregatedData,
    InsightsReport,
)
from opencortex.insights.agent import InsightsAgent
from opencortex.insights.collector import InsightsCollector
from opencortex.insights.report import ReportManager

__all__ = [
    "InsightsAgent",
    "InsightsCollector",
    "ReportManager",
    "SessionMeta",
    "SessionFacet",
    "AggregatedData",
    "InsightsReport",
]
