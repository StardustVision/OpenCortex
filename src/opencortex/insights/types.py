"""Core data types for CC-equivalent insights analysis."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class SessionMeta:
    """Per-session metrics extracted by SessionMetaExtractor (zero LLM)."""

    session_id: str
    tenant_id: str
    user_id: str
    project_path: str
    start_time: str
    duration_minutes: float
    user_message_count: int
    assistant_message_count: int
    tool_counts: Dict[str, int]
    languages: Dict[str, int]
    git_commits: int
    git_pushes: int
    input_tokens: int
    output_tokens: int
    first_prompt: str
    summary: Optional[str] = None
    user_interruptions: int = 0
    user_response_times: List[float] = field(default_factory=list)
    tool_errors: int = 0
    tool_error_categories: Dict[str, int] = field(default_factory=dict)
    uses_agent: bool = False
    uses_mcp: bool = False
    uses_web_search: bool = False
    uses_web_fetch: bool = False
    lines_added: int = 0
    lines_removed: int = 0
    files_modified: int = 0
    message_hours: List[int] = field(default_factory=list)
    user_message_timestamps: List[str] = field(default_factory=list)


@dataclass
class SessionFacet:
    """LLM-extracted qualitative analysis of a session (CC-equivalent)."""

    session_id: str
    underlying_goal: str
    goal_categories: Dict[str, int]
    outcome: str
    user_satisfaction_counts: Dict[str, int]
    claude_helpfulness: str
    session_type: str
    friction_counts: Dict[str, int] = field(default_factory=dict)
    friction_detail: str = ""
    primary_success: str = "none"
    brief_summary: str = ""
    user_instructions_to_claude: List[str] = field(default_factory=list)


@dataclass
class AggregatedData:
    """Cross-session aggregation (CC-equivalent, 40+ fields)."""

    total_sessions: int
    total_sessions_scanned: int
    sessions_with_facets: int
    date_range: Dict[str, str]
    total_messages: int
    total_duration_hours: float
    total_input_tokens: int
    total_output_tokens: int
    tool_counts: Dict[str, int]
    languages: Dict[str, int]
    git_commits: int
    git_pushes: int
    projects: Dict[str, int]
    goal_categories: Dict[str, int]
    outcomes: Dict[str, int]
    satisfaction: Dict[str, int]
    helpfulness: Dict[str, int]
    session_types: Dict[str, int]
    friction: Dict[str, int]
    success: Dict[str, int]
    session_summaries: List[Dict[str, str]]
    total_interruptions: int
    total_tool_errors: int
    tool_error_categories: Dict[str, int]
    user_response_times: List[float]
    median_response_time: float
    avg_response_time: float
    sessions_using_agent: int
    sessions_using_mcp: int
    sessions_using_web_search: int
    sessions_using_web_fetch: int
    total_lines_added: int
    total_lines_removed: int
    total_files_modified: int
    days_active: int
    messages_per_day: float
    message_hours: List[int]
    multi_clauding: Dict[str, int]


@dataclass
class InsightsReport:
    """Complete insights report (CC-equivalent with enriched sections)."""

    tenant_id: str
    user_id: str
    report_period: str
    generated_at: datetime
    total_sessions: int
    total_messages: int
    total_duration_hours: float
    session_facets: List[SessionFacet] = field(default_factory=list)
    project_areas: Dict[str, Any] = field(default_factory=dict)
    what_works: List[str] = field(default_factory=list)
    friction_analysis: Dict[str, int] = field(default_factory=dict)
    suggestions: List[str] = field(default_factory=list)
    on_the_horizon: List[str] = field(default_factory=list)
    at_a_glance: Dict[str, str] = field(default_factory=dict)
    interaction_style: Optional[Dict[str, str]] = None
    what_works_detail: Optional[Dict[str, Any]] = None
    friction_detail: Optional[Dict[str, Any]] = None
    suggestions_detail: Optional[Dict[str, Any]] = None
    on_the_horizon_detail: Optional[Dict[str, Any]] = None
    fun_ending: Optional[Dict[str, str]] = None
    aggregated: Optional[Dict[str, Any]] = None
    cache_hits: int = 0
    llm_calls: int = 0
