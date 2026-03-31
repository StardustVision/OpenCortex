"""Insights module for analyzing user behavior and session patterns."""

from opencortex.insights.types import (
    SessionMeta,
    SessionFacet,
    AggregatedData,
    InsightsReport,
)

# Sub-modules that depend on old types are imported lazily to avoid import
# errors during the staged rewrite (Tasks 4+). Once collector/agent/report are
# updated they will be re-added here.
try:
    from opencortex.insights.agent import InsightsAgent
    from opencortex.insights.collector import InsightsCollector
    from opencortex.insights.report import ReportManager
except ImportError:
    InsightsAgent = None  # type: ignore[assignment,misc]
    InsightsCollector = None  # type: ignore[assignment,misc]
    ReportManager = None  # type: ignore[assignment,misc]

__all__ = [
    "InsightsAgent",
    "InsightsCollector",
    "ReportManager",
    "SessionMeta",
    "SessionFacet",
    "AggregatedData",
    "InsightsReport",
]
