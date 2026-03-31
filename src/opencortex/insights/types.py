"""Core data types for insights analysis."""

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, List, Dict, Set, Any


@dataclass
class SessionRecord:
    """Unified session record for insights analysis."""

    session_id: str
    tenant_id: str
    user_id: str
    project_id: str
    started_at: datetime
    ended_at: datetime
    message_count: int
    user_message_count: int
    tool_calls: int
    memories_created: int
    memories_referenced: int
    feedback_given: int
    session_type: str
    outcome: str


@dataclass
class UserActivityWindow:
    """Time window for aggregating user activity."""

    start_date: date
    end_date: date
    sessions: int
    total_messages: int
    total_tokens: int
    unique_projects: int
    tool_usage: Dict[str, int] = field(default_factory=dict)
    memory_feedback_score: float = 0.0


@dataclass
class SessionFacet:
    """Structured analysis of a single session."""

    session_id: str
    underlying_goal: str
    brief_summary: str
    goal_categories: List[str]
    outcome: str
    user_satisfaction_counts: Dict[str, int]
    claude_helpfulness: float
    session_type: str
    friction_counts: Dict[str, int] = field(default_factory=dict)
    friction_detail: List[Dict[str, Any]] = field(default_factory=list)
    primary_success: Optional[str] = None


@dataclass
class InsightsReport:
    """Complete insights report for a user."""

    tenant_id: str
    user_id: str
    report_period: str
    generated_at: datetime
    total_sessions: int
    total_messages: int
    total_duration_hours: float
    session_facets: List[SessionFacet] = field(default_factory=list)
    project_areas: Dict[str, int] = field(default_factory=dict)
    what_works: List[str] = field(default_factory=list)
    friction_analysis: Dict[str, int] = field(default_factory=dict)
    suggestions: List[str] = field(default_factory=list)
    on_the_horizon: List[str] = field(default_factory=list)
    at_a_glance: str = ""
    cache_hits: int = 0
    llm_calls: int = 0
