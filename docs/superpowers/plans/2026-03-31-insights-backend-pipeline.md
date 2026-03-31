# Insights Backend Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the OC insights backend to match Claude Code's /insights data depth, analysis quality, and caching — delivering a CC-equivalent 8-phase pipeline.

**Architecture:** Pure-code SessionMetaExtractor (30-field SessionMeta, zero LLM) → CortexFS-backed cache → CC-quality facet prompts with concurrent extraction → AggregatedData (40+ fields) → 7 parallel sections + serial at_a_glance → enriched JSON report.

**Tech Stack:** Python 3.10+ async, Qdrant (via TraceStore), CortexFS, asyncio.gather for parallelism, unittest for tests.

**Spec:** `docs/superpowers/specs/2026-03-31-insights-cc-replication-design.md`

**Scope:** Backend only (Layers 1-3). Frontend dashboard is a separate plan.

**Deferred:** Observer enhancement (ToolCallDetail with input_params, is_error, etc.) requires MCP plugin changes (Node.js) and is deferred. The SessionMetaExtractor handles missing fields gracefully — it extracts what's available in the current `tool_calls` structure and skips fields that require enriched data (file paths, error text, etc.). Once Observer is enhanced in a future plan, the extractor automatically benefits without code changes.

---

## Dependency Graph

```
Task 1 (constants) ─┐
Task 2 (labels)    ─┤
Task 3 (types)     ─┼─→ Task 5 (extractor) ─┐
                    │                         │
                    ├─→ Task 6 (cache)       ─┤
Task 4 (multi-cl.) ─┤                         ├─→ Task 8 (agent) → Task 9 (report+API)
                    │                         │
Task 7 (prompts)   ─┴─────────────────────── ┘
```

Tasks 1, 2, 4, 7 are fully independent. Task 3 is independent but a prerequisite for Tasks 5, 6, 8.

---

## File Map

### New files
| File | Responsibility |
|------|---------------|
| `src/opencortex/insights/constants.py` | All numeric constants, bucket definitions, display orders |
| `src/opencortex/insights/labels.py` | LABEL_MAP (40+ key→display mappings) + `label()` helper |
| `src/opencortex/insights/extractor.py` | SessionMetaExtractor — pure-code metric extraction from Trace |
| `src/opencortex/insights/cache.py` | InsightsCache — CortexFS-backed meta + facet cache |
| `src/opencortex/insights/multi_clauding.py` | `detect_multi_clauding()` sliding window algorithm |
| `tests/insights/test_constants.py` | Constants validation |
| `tests/insights/test_labels.py` | Label mapping tests |
| `tests/insights/test_extractor.py` | SessionMetaExtractor unit tests |
| `tests/insights/test_cache.py` | InsightsCache tests |
| `tests/insights/test_multi_clauding.py` | Multi-clauding detection tests |

### Modified files
| File | What changes |
|------|-------------|
| `src/opencortex/insights/types.py` | Replace SessionRecord/UserActivityWindow with SessionMeta/AggregatedData; rewrite SessionFacet/InsightsReport |
| `src/opencortex/insights/prompts.py` | Replace all 8 prompts with CC-equivalent versions (9 total) |
| `src/opencortex/insights/agent.py` | Rewrite to 8-phase pipeline with parallel execution |
| `src/opencortex/insights/collector.py` | Simplify to trace-fetching only (extraction moves to extractor.py) |
| `src/opencortex/insights/report.py` | Update serialization for enriched InsightsReport |
| `src/opencortex/insights/api.py` | Update /generate response for enriched data |
| `tests/insights/test_types.py` | Rewrite for new type definitions |
| `tests/insights/test_agent.py` | Rewrite for 8-phase pipeline |
| `tests/insights/test_prompts.py` | Update for 9 prompts |

---

### Task 1: Constants Module

**Files:**
- Create: `src/opencortex/insights/constants.py`
- Test: `tests/insights/test_constants.py`

- [ ] **Step 1: Write test**

```python
# tests/insights/test_constants.py
"""Tests for insights constants."""
import unittest
from opencortex.insights.constants import (
    MAX_SESSIONS_TO_LOAD,
    MAX_FACET_EXTRACTIONS,
    FACET_CONCURRENCY,
    TRANSCRIPT_THRESHOLD,
    CHUNK_SIZE,
    OVERLAP_WINDOW_MS,
    MIN_RESPONSE_TIME_SEC,
    MAX_RESPONSE_TIME_SEC,
    MIN_USER_MESSAGES,
    MIN_DURATION_MINUTES,
    RESPONSE_TIME_BUCKETS,
    SATISFACTION_ORDER,
    OUTCOME_ORDER,
    EXTENSION_TO_LANGUAGE,
    ERROR_CATEGORIES,
)


class TestConstants(unittest.TestCase):
    def test_session_limits(self):
        self.assertEqual(MAX_SESSIONS_TO_LOAD, 200)
        self.assertEqual(MAX_FACET_EXTRACTIONS, 50)
        self.assertEqual(FACET_CONCURRENCY, 50)

    def test_transcript_thresholds(self):
        self.assertEqual(TRANSCRIPT_THRESHOLD, 30000)
        self.assertEqual(CHUNK_SIZE, 25000)
        self.assertLess(CHUNK_SIZE, TRANSCRIPT_THRESHOLD)

    def test_response_time_buckets_cover_full_range(self):
        self.assertEqual(RESPONSE_TIME_BUCKETS[0][0], "2-10s")
        self.assertEqual(RESPONSE_TIME_BUCKETS[-1][0], ">15m")
        # Buckets are contiguous
        for i in range(len(RESPONSE_TIME_BUCKETS) - 1):
            self.assertEqual(RESPONSE_TIME_BUCKETS[i][2], RESPONSE_TIME_BUCKETS[i + 1][1])

    def test_satisfaction_order(self):
        self.assertEqual(SATISFACTION_ORDER[0], "frustrated")
        self.assertEqual(SATISFACTION_ORDER[-1], "unsure")
        self.assertEqual(len(SATISFACTION_ORDER), 6)

    def test_outcome_order(self):
        self.assertEqual(OUTCOME_ORDER[0], "not_achieved")
        self.assertEqual(OUTCOME_ORDER[-1], "unclear_from_transcript")
        self.assertEqual(len(OUTCOME_ORDER), 5)

    def test_extension_to_language(self):
        self.assertEqual(EXTENSION_TO_LANGUAGE[".py"], "Python")
        self.assertEqual(EXTENSION_TO_LANGUAGE[".ts"], "TypeScript")
        self.assertEqual(EXTENSION_TO_LANGUAGE[".tsx"], "TypeScript")
        self.assertGreaterEqual(len(EXTENSION_TO_LANGUAGE), 16)

    def test_error_categories(self):
        names = [cat[-1] for cat in ERROR_CATEGORIES]
        self.assertIn("Command Failed", names)
        self.assertIn("Edit Failed", names)
        self.assertIn("File Not Found", names)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/insights/test_constants.py -v`
Expected: FAIL (ModuleNotFoundError: No module named 'opencortex.insights.constants')

- [ ] **Step 3: Implement constants module**

```python
# src/opencortex/insights/constants.py
"""All numeric constants, bucket definitions, and display orders for insights."""

from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# Session loading limits
# ---------------------------------------------------------------------------
MAX_SESSIONS_TO_LOAD = 200
MAX_FACET_EXTRACTIONS = 50
FACET_CONCURRENCY = 50
META_BATCH_SIZE = 50

# ---------------------------------------------------------------------------
# Transcript processing
# ---------------------------------------------------------------------------
TRANSCRIPT_THRESHOLD = 30000  # chars: summarize if over
CHUNK_SIZE = 25000            # chars per chunk

# ---------------------------------------------------------------------------
# Multi-clauding
# ---------------------------------------------------------------------------
OVERLAP_WINDOW_MS = 30 * 60 * 1000  # 30 minutes

# ---------------------------------------------------------------------------
# Response time
# ---------------------------------------------------------------------------
MIN_RESPONSE_TIME_SEC = 2
MAX_RESPONSE_TIME_SEC = 3600

# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
MIN_USER_MESSAGES = 2
MIN_DURATION_MINUTES = 1

# ---------------------------------------------------------------------------
# Response time histogram buckets: (label, lower_bound, upper_bound)
# ---------------------------------------------------------------------------
RESPONSE_TIME_BUCKETS: List[Tuple[str, float, float]] = [
    ("2-10s",  2,   10),
    ("10-30s", 10,  30),
    ("30s-1m", 30,  60),
    ("1-2m",   60,  120),
    ("2-5m",   120, 300),
    ("5-15m",  300, 900),
    (">15m",   900, float("inf")),
]

# ---------------------------------------------------------------------------
# Display orders (fixed sort for charts)
# ---------------------------------------------------------------------------
SATISFACTION_ORDER: List[str] = [
    "frustrated", "dissatisfied", "likely_satisfied",
    "satisfied", "happy", "unsure",
]

OUTCOME_ORDER: List[str] = [
    "not_achieved", "partially_achieved", "mostly_achieved",
    "fully_achieved", "unclear_from_transcript",
]

# ---------------------------------------------------------------------------
# Language mapping (file extension → language name)
# ---------------------------------------------------------------------------
EXTENSION_TO_LANGUAGE: Dict[str, str] = {
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript",
    ".py": "Python", ".rb": "Ruby", ".go": "Go",
    ".rs": "Rust", ".java": "Java", ".md": "Markdown",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML",
    ".sh": "Shell", ".css": "CSS", ".html": "HTML",
}

# ---------------------------------------------------------------------------
# Error classification rules: (keywords_tuple, category_name)
# Each tuple entry: if ANY keyword in the tuple matches, assign the category.
# ---------------------------------------------------------------------------
ERROR_CATEGORIES: List[Tuple[Tuple[str, ...], str]] = [
    (("exit code",),                                    "Command Failed"),
    (("rejected", "doesn't want"),                      "User Rejected"),
    (("string to replace not found", "no changes"),     "Edit Failed"),
    (("modified since read",),                          "File Changed"),
    (("exceeds maximum", "too large"),                  "File Too Large"),
    (("file not found", "does not exist"),              "File Not Found"),
]
# Default category when no keyword matches: "Other"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/insights/test_constants.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/insights/constants.py tests/insights/test_constants.py
git commit -m "feat(insights): add constants module with CC-equivalent thresholds"
```

---

### Task 2: Labels Module

**Files:**
- Create: `src/opencortex/insights/labels.py`
- Test: `tests/insights/test_labels.py`

- [ ] **Step 1: Write test**

```python
# tests/insights/test_labels.py
"""Tests for insights label mapping."""
import unittest
from opencortex.insights.labels import LABEL_MAP, label


class TestLabels(unittest.TestCase):
    def test_goal_categories(self):
        self.assertEqual(label("debug_investigate"), "Debug/Investigate")
        self.assertEqual(label("implement_feature"), "Implement Feature")
        self.assertEqual(label("warmup_minimal"), "Cache Warmup")

    def test_friction_types(self):
        self.assertEqual(label("misunderstood_request"), "Misunderstood Request")
        self.assertEqual(label("wrong_approach"), "Wrong Approach")
        self.assertEqual(label("excessive_changes"), "Excessive Changes")

    def test_satisfaction(self):
        self.assertEqual(label("frustrated"), "Frustrated")
        self.assertEqual(label("likely_satisfied"), "Likely Satisfied")
        self.assertEqual(label("delighted"), "Delighted")

    def test_outcomes(self):
        self.assertEqual(label("fully_achieved"), "Fully Achieved")
        self.assertEqual(label("unclear_from_transcript"), "Unclear")

    def test_helpfulness(self):
        self.assertEqual(label("essential"), "Essential")
        self.assertEqual(label("slightly_helpful"), "Slightly Helpful")

    def test_unknown_key_fallback(self):
        self.assertEqual(label("some_unknown_key"), "Some Unknown Key")

    def test_label_map_size(self):
        self.assertGreaterEqual(len(LABEL_MAP), 40)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/insights/test_labels.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement labels module**

```python
# src/opencortex/insights/labels.py
"""Display-name mapping for all insight category keys."""

from typing import Dict

LABEL_MAP: Dict[str, str] = {
    # Goal categories
    "debug_investigate": "Debug/Investigate",
    "implement_feature": "Implement Feature",
    "fix_bug": "Fix Bug",
    "write_script_tool": "Write Script/Tool",
    "refactor_code": "Refactor Code",
    "configure_system": "Configure System",
    "create_pr_commit": "Create PR/Commit",
    "analyze_data": "Analyze Data",
    "understand_codebase": "Understand Codebase",
    "write_tests": "Write Tests",
    "write_docs": "Write Docs",
    "deploy_infra": "Deploy/Infra",
    "warmup_minimal": "Cache Warmup",
    # Success factors
    "fast_accurate_search": "Fast/Accurate Search",
    "correct_code_edits": "Correct Code Edits",
    "good_explanations": "Good Explanations",
    "proactive_help": "Proactive Help",
    "multi_file_changes": "Multi-file Changes",
    "handled_complexity": "Multi-file Changes",
    "good_debugging": "Good Debugging",
    # Friction types
    "misunderstood_request": "Misunderstood Request",
    "wrong_approach": "Wrong Approach",
    "buggy_code": "Buggy Code",
    "user_rejected_action": "User Rejected Action",
    "claude_got_blocked": "Claude Got Blocked",
    "user_stopped_early": "User Stopped Early",
    "wrong_file_or_location": "Wrong File/Location",
    "excessive_changes": "Excessive Changes",
    "slow_or_verbose": "Slow/Verbose",
    "tool_failed": "Tool Failed",
    "user_unclear": "User Unclear",
    "external_issue": "External Issue",
    # Satisfaction
    "frustrated": "Frustrated",
    "dissatisfied": "Dissatisfied",
    "likely_satisfied": "Likely Satisfied",
    "satisfied": "Satisfied",
    "happy": "Happy",
    "unsure": "Unsure",
    "neutral": "Neutral",
    "delighted": "Delighted",
    # Session types
    "single_task": "Single Task",
    "multi_task": "Multi Task",
    "iterative_refinement": "Iterative Refinement",
    "exploration": "Exploration",
    "quick_question": "Quick Question",
    # Outcomes
    "fully_achieved": "Fully Achieved",
    "mostly_achieved": "Mostly Achieved",
    "partially_achieved": "Partially Achieved",
    "not_achieved": "Not Achieved",
    "unclear_from_transcript": "Unclear",
    # Helpfulness
    "unhelpful": "Unhelpful",
    "slightly_helpful": "Slightly Helpful",
    "moderately_helpful": "Moderately Helpful",
    "very_helpful": "Very Helpful",
    "essential": "Essential",
}


def label(key: str) -> str:
    """Return display name for a category key. Falls back to title-cased key."""
    return LABEL_MAP.get(key, key.replace("_", " ").title())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/insights/test_labels.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/insights/labels.py tests/insights/test_labels.py
git commit -m "feat(insights): add label map with 40+ CC-equivalent display names"
```

---

### Task 3: Types Rewrite

**Files:**
- Modify: `src/opencortex/insights/types.py`
- Modify: `tests/insights/test_types.py`

This replaces the old types (SessionRecord, UserActivityWindow) with CC-equivalent ones (SessionMeta, AggregatedData) and rewrites SessionFacet and InsightsReport.

- [ ] **Step 1: Write test for new types**

```python
# tests/insights/test_types.py
"""Tests for insights core data types (CC-equivalent)."""
import unittest
from dataclasses import asdict
from datetime import datetime
from opencortex.insights.types import (
    SessionMeta,
    SessionFacet,
    AggregatedData,
    InsightsReport,
)


class TestSessionMeta(unittest.TestCase):
    def test_creation_all_fields(self):
        meta = SessionMeta(
            session_id="s1",
            tenant_id="t1",
            user_id="u1",
            project_path="/project",
            start_time="2026-03-31T10:00:00",
            duration_minutes=30.0,
            user_message_count=10,
            assistant_message_count=12,
            tool_counts={"Read": 5, "Edit": 3},
            languages={"Python": 8},
            git_commits=2,
            git_pushes=1,
            input_tokens=5000,
            output_tokens=8000,
            first_prompt="Fix the auth bug",
        )
        self.assertEqual(meta.session_id, "s1")
        self.assertEqual(meta.tool_counts["Read"], 5)
        self.assertEqual(meta.git_commits, 2)

    def test_defaults(self):
        meta = SessionMeta(
            session_id="s1", tenant_id="t1", user_id="u1",
            project_path="", start_time="", duration_minutes=0,
            user_message_count=0, assistant_message_count=0,
            tool_counts={}, languages={},
            git_commits=0, git_pushes=0,
            input_tokens=0, output_tokens=0, first_prompt="",
        )
        self.assertEqual(meta.user_interruptions, 0)
        self.assertEqual(meta.user_response_times, [])
        self.assertFalse(meta.uses_agent)
        self.assertEqual(meta.lines_added, 0)
        self.assertEqual(meta.message_hours, [])
        self.assertEqual(meta.user_message_timestamps, [])

    def test_serialization_roundtrip(self):
        meta = SessionMeta(
            session_id="s1", tenant_id="t1", user_id="u1",
            project_path="/p", start_time="2026-01-01T00:00:00",
            duration_minutes=5, user_message_count=2,
            assistant_message_count=3, tool_counts={"Bash": 1},
            languages={"Go": 1}, git_commits=0, git_pushes=0,
            input_tokens=100, output_tokens=200, first_prompt="hi",
        )
        d = asdict(meta)
        restored = SessionMeta(**d)
        self.assertEqual(restored.session_id, "s1")
        self.assertEqual(restored.tool_counts, {"Bash": 1})

    def test_mutable_defaults_independent(self):
        m1 = SessionMeta(
            session_id="a", tenant_id="t", user_id="u",
            project_path="", start_time="", duration_minutes=0,
            user_message_count=0, assistant_message_count=0,
            tool_counts={}, languages={},
            git_commits=0, git_pushes=0,
            input_tokens=0, output_tokens=0, first_prompt="",
        )
        m2 = SessionMeta(
            session_id="b", tenant_id="t", user_id="u",
            project_path="", start_time="", duration_minutes=0,
            user_message_count=0, assistant_message_count=0,
            tool_counts={}, languages={},
            git_commits=0, git_pushes=0,
            input_tokens=0, output_tokens=0, first_prompt="",
        )
        m1.user_response_times.append(5.0)
        self.assertEqual(m2.user_response_times, [])


class TestSessionFacet(unittest.TestCase):
    def test_cc_equivalent_types(self):
        """goal_categories must be Dict[str,int], claude_helpfulness must be str."""
        facet = SessionFacet(
            session_id="s1",
            underlying_goal="Implement auth",
            goal_categories={"implement_feature": 1, "fix_bug": 1},
            outcome="fully_achieved",
            user_satisfaction_counts={"satisfied": 2},
            claude_helpfulness="very_helpful",
            session_type="multi_task",
        )
        self.assertIsInstance(facet.goal_categories, dict)
        self.assertEqual(facet.goal_categories["implement_feature"], 1)
        self.assertIsInstance(facet.claude_helpfulness, str)
        self.assertEqual(facet.friction_detail, "")
        self.assertEqual(facet.primary_success, "none")
        self.assertEqual(facet.user_instructions_to_claude, [])

    def test_mutable_defaults(self):
        f1 = SessionFacet(
            session_id="a", underlying_goal="g",
            goal_categories={}, outcome="unclear_from_transcript",
            user_satisfaction_counts={}, claude_helpfulness="moderately_helpful",
            session_type="single_task",
        )
        f2 = SessionFacet(
            session_id="b", underlying_goal="g",
            goal_categories={}, outcome="unclear_from_transcript",
            user_satisfaction_counts={}, claude_helpfulness="moderately_helpful",
            session_type="single_task",
        )
        f1.friction_counts["buggy_code"] = 3
        self.assertEqual(f2.friction_counts, {})


class TestAggregatedData(unittest.TestCase):
    def test_creation(self):
        agg = AggregatedData(
            total_sessions=10, total_sessions_scanned=15,
            sessions_with_facets=8,
            date_range={"start": "2026-03-01", "end": "2026-03-31"},
            total_messages=200, total_duration_hours=15.5,
            total_input_tokens=50000, total_output_tokens=80000,
            tool_counts={"Read": 100}, languages={"Python": 50},
            git_commits=20, git_pushes=5,
            projects={"/proj": 10},
            goal_categories={"implement_feature": 5},
            outcomes={"fully_achieved": 7},
            satisfaction={"satisfied": 6},
            helpfulness={"very_helpful": 5},
            session_types={"multi_task": 4},
            friction={"wrong_approach": 3},
            success={"correct_code_edits": 4},
            session_summaries=[],
            total_interruptions=2,
            total_tool_errors=5,
            tool_error_categories={"Command Failed": 3},
            user_response_times=[5.0, 10.0],
            median_response_time=7.5,
            avg_response_time=7.5,
            sessions_using_agent=3,
            sessions_using_mcp=1,
            sessions_using_web_search=0,
            sessions_using_web_fetch=0,
            total_lines_added=500,
            total_lines_removed=200,
            total_files_modified=30,
            days_active=20,
            messages_per_day=10.0,
            message_hours=[9, 10, 14, 15],
            multi_clauding={"overlap_events": 0, "sessions_involved": 0, "user_messages_during": 0},
        )
        self.assertEqual(agg.total_sessions, 10)
        self.assertEqual(agg.median_response_time, 7.5)
        self.assertEqual(agg.multi_clauding["overlap_events"], 0)


class TestInsightsReport(unittest.TestCase):
    def test_enriched_report(self):
        report = InsightsReport(
            tenant_id="t1", user_id="u1",
            report_period="2026-03-01 - 2026-03-31",
            generated_at=datetime(2026, 3, 31, 12, 0, 0),
            total_sessions=10, total_messages=200,
            total_duration_hours=15.5,
        )
        self.assertEqual(report.at_a_glance, {})
        self.assertIsNone(report.interaction_style)
        self.assertIsNone(report.fun_ending)
        self.assertEqual(report.llm_calls, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/insights/test_types.py -v`
Expected: FAIL (cannot import SessionMeta, AggregatedData)

- [ ] **Step 3: Rewrite types.py**

```python
# src/opencortex/insights/types.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/insights/test_types.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/insights/types.py tests/insights/test_types.py
git commit -m "feat(insights): rewrite types with CC-equivalent SessionMeta, SessionFacet, AggregatedData"
```

---

### Task 4: Multi-Clauding Detection

**Files:**
- Create: `src/opencortex/insights/multi_clauding.py`
- Test: `tests/insights/test_multi_clauding.py`

- [ ] **Step 1: Write test**

```python
# tests/insights/test_multi_clauding.py
"""Tests for multi-clauding sliding window detection."""
import unittest
from opencortex.insights.multi_clauding import detect_multi_clauding
from opencortex.insights.types import SessionMeta


def _make_meta(sid: str, timestamps: list) -> SessionMeta:
    return SessionMeta(
        session_id=sid, tenant_id="t", user_id="u",
        project_path="", start_time="", duration_minutes=0,
        user_message_count=len(timestamps), assistant_message_count=0,
        tool_counts={}, languages={},
        git_commits=0, git_pushes=0,
        input_tokens=0, output_tokens=0, first_prompt="",
        user_message_timestamps=timestamps,
    )


class TestMultiClauding(unittest.TestCase):
    def test_no_overlap(self):
        """Two sessions that don't overlap → no multi-clauding."""
        s1 = _make_meta("s1", ["2026-03-31T10:00:00", "2026-03-31T10:05:00"])
        s2 = _make_meta("s2", ["2026-03-31T11:00:00", "2026-03-31T11:05:00"])
        result = detect_multi_clauding([s1, s2])
        self.assertEqual(result["overlap_events"], 0)
        self.assertEqual(result["sessions_involved"], 0)

    def test_interleaved_sessions(self):
        """s1 → s2 → s1 within 30 min → one overlap event."""
        s1 = _make_meta("s1", ["2026-03-31T10:00:00", "2026-03-31T10:10:00"])
        s2 = _make_meta("s2", ["2026-03-31T10:05:00"])
        result = detect_multi_clauding([s1, s2])
        self.assertGreaterEqual(result["overlap_events"], 1)
        self.assertEqual(result["sessions_involved"], 2)
        self.assertGreater(result["user_messages_during"], 0)

    def test_single_session(self):
        """Single session cannot multi-claude."""
        s1 = _make_meta("s1", ["2026-03-31T10:00:00", "2026-03-31T10:05:00"])
        result = detect_multi_clauding([s1])
        self.assertEqual(result["overlap_events"], 0)

    def test_empty(self):
        result = detect_multi_clauding([])
        self.assertEqual(result["overlap_events"], 0)
        self.assertEqual(result["sessions_involved"], 0)
        self.assertEqual(result["user_messages_during"], 0)

    def test_outside_window(self):
        """s1 → s2 → s1 but >30 min apart → no overlap."""
        s1 = _make_meta("s1", ["2026-03-31T10:00:00", "2026-03-31T11:00:00"])
        s2 = _make_meta("s2", ["2026-03-31T10:35:00"])
        result = detect_multi_clauding([s1, s2])
        self.assertEqual(result["overlap_events"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/insights/test_multi_clauding.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement multi-clauding detection**

```python
# src/opencortex/insights/multi_clauding.py
"""Sliding window detection of concurrent session usage (CC-equivalent)."""

from datetime import datetime
from typing import Any, Dict, List

from opencortex.insights.constants import OVERLAP_WINDOW_MS
from opencortex.insights.types import SessionMeta


def detect_multi_clauding(sessions: List[SessionMeta]) -> Dict[str, int]:
    """Detect pattern: session1 → session2 → session1 within 30-min window."""
    all_messages: List[Dict[str, Any]] = []
    for meta in sessions:
        for ts_str in meta.user_message_timestamps:
            try:
                ts = datetime.fromisoformat(ts_str).timestamp() * 1000
                all_messages.append({"ts": ts, "session_id": meta.session_id})
            except (ValueError, TypeError):
                continue

    all_messages.sort(key=lambda m: m["ts"])

    session_pairs: set = set()
    messages_during: set = set()
    window_start = 0
    session_last_index: Dict[str, int] = {}

    for i, msg in enumerate(all_messages):
        # Shrink window from the left
        while (
            window_start < i
            and msg["ts"] - all_messages[window_start]["ts"] > OVERLAP_WINDOW_MS
        ):
            expiring = all_messages[window_start]
            if session_last_index.get(expiring["session_id"]) == window_start:
                del session_last_index[expiring["session_id"]]
            window_start += 1

        # Check for interleaving
        prev_idx = session_last_index.get(msg["session_id"])
        if prev_idx is not None:
            for j in range(prev_idx + 1, i):
                between = all_messages[j]
                if between["session_id"] != msg["session_id"]:
                    pair = tuple(sorted([msg["session_id"], between["session_id"]]))
                    session_pairs.add(pair)
                    messages_during.add(f"{all_messages[prev_idx]['ts']}:{msg['session_id']}")
                    messages_during.add(f"{between['ts']}:{between['session_id']}")
                    messages_during.add(f"{msg['ts']}:{msg['session_id']}")
                    break

        session_last_index[msg["session_id"]] = i

    involved: set = set()
    for s1, s2 in session_pairs:
        involved.add(s1)
        involved.add(s2)

    return {
        "overlap_events": len(session_pairs),
        "sessions_involved": len(involved),
        "user_messages_during": len(messages_during),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/insights/test_multi_clauding.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/insights/multi_clauding.py tests/insights/test_multi_clauding.py
git commit -m "feat(insights): add multi-clauding detection with 30-min sliding window"
```

---

### Task 5: SessionMetaExtractor

**Files:**
- Create: `src/opencortex/insights/extractor.py`
- Test: `tests/insights/test_extractor.py`

**Depends on:** Task 1 (constants), Task 3 (types)

- [ ] **Step 1: Write test**

```python
# tests/insights/test_extractor.py
"""Tests for SessionMetaExtractor — pure-code metric extraction."""
import unittest
from opencortex.alpha.types import Turn, Trace, TurnStatus
from opencortex.insights.extractor import SessionMetaExtractor


def _make_trace(turns: list, session_id: str = "s1") -> Trace:
    return Trace(
        trace_id="tr1", session_id=session_id,
        tenant_id="t1", user_id="u1", source="claude_code",
        turns=turns, created_at="2026-03-31T10:00:00+00:00",
    )


class TestSessionMetaExtractor(unittest.TestCase):
    def setUp(self):
        self.extractor = SessionMetaExtractor()

    def test_tool_counting(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Read", "summary": "read file"},
                {"name": "Edit", "summary": "edit file", "input_params": {"file_path": "/src/app.py"}},
            ]),
            Turn(turn_id="2", tool_calls=[
                {"name": "Read", "summary": "read another"},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.tool_counts["Read"], 2)
        self.assertEqual(meta.tool_counts["Edit"], 1)

    def test_language_detection(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Edit", "input_params": {"file_path": "/src/app.py"}},
                {"name": "Edit", "input_params": {"file_path": "/src/index.ts"}},
                {"name": "Edit", "input_params": {"file_path": "/src/utils.ts"}},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.languages["Python"], 1)
        self.assertEqual(meta.languages["TypeScript"], 2)

    def test_git_detection(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Bash", "input_params": {"command": "git commit -m 'fix'"}},
                {"name": "Bash", "input_params": {"command": "git push origin main"}},
                {"name": "Bash", "input_params": {"command": "ls -la"}},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.git_commits, 1)
        self.assertEqual(meta.git_pushes, 1)

    def test_error_classification(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Bash", "is_error": True, "error_text": "exit code 1"},
                {"name": "Edit", "is_error": True, "error_text": "string to replace not found"},
                {"name": "Read", "is_error": True, "error_text": "file not found: /missing.py"},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.tool_errors, 3)
        self.assertEqual(meta.tool_error_categories["Command Failed"], 1)
        self.assertEqual(meta.tool_error_categories["Edit Failed"], 1)
        self.assertEqual(meta.tool_error_categories["File Not Found"], 1)

    def test_special_tool_detection(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Agent", "summary": "sub-agent"},
                {"name": "mcp__memory_store", "summary": "store"},
                {"name": "WebSearch", "summary": "search"},
                {"name": "WebFetch", "summary": "fetch"},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertTrue(meta.uses_agent)
        self.assertTrue(meta.uses_mcp)
        self.assertTrue(meta.uses_web_search)
        self.assertTrue(meta.uses_web_fetch)

    def test_user_interruption_count(self):
        turns = [
            Turn(turn_id="1", turn_status=TurnStatus.INTERRUPTED),
            Turn(turn_id="2", turn_status=TurnStatus.COMPLETE),
            Turn(turn_id="3", turn_status=TurnStatus.INTERRUPTED),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.user_interruptions, 2)

    def test_message_counting(self):
        turns = [
            Turn(turn_id="1", prompt_text="Hello"),
            Turn(turn_id="2", prompt_text="Fix bug", final_text="Done"),
            Turn(turn_id="3", final_text="Here's the result"),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.user_message_count, 2)
        self.assertEqual(meta.assistant_message_count, 2)

    def test_files_modified(self):
        turns = [
            Turn(turn_id="1", tool_calls=[
                {"name": "Edit", "input_params": {"file_path": "/a.py"}},
                {"name": "Write", "input_params": {"file_path": "/b.py"}},
                {"name": "Edit", "input_params": {"file_path": "/a.py"}},
            ]),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.files_modified, 2)

    def test_first_prompt(self):
        turns = [
            Turn(turn_id="1", prompt_text="Fix the authentication bug in login.py"),
        ]
        meta = self.extractor.extract(_make_trace(turns))
        self.assertEqual(meta.first_prompt, "Fix the authentication bug in login.py")

    def test_empty_trace(self):
        meta = self.extractor.extract(_make_trace([]))
        self.assertEqual(meta.tool_counts, {})
        self.assertEqual(meta.user_message_count, 0)
        self.assertEqual(meta.first_prompt, "")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/insights/test_extractor.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement extractor**

```python
# src/opencortex/insights/extractor.py
"""SessionMetaExtractor — pure-code metric extraction from Trace turns (zero LLM)."""

import os
from datetime import datetime
from typing import Any, Dict, List, Set

from opencortex.alpha.types import Trace, Turn, TurnStatus
from opencortex.insights.constants import (
    EXTENSION_TO_LANGUAGE,
    ERROR_CATEGORIES,
    MIN_RESPONSE_TIME_SEC,
    MAX_RESPONSE_TIME_SEC,
)
from opencortex.insights.types import SessionMeta


class SessionMetaExtractor:
    """Extract structured metrics from a Trace. Pure code, zero LLM."""

    def extract(self, trace: Trace) -> SessionMeta:
        tool_counts: Dict[str, int] = {}
        languages: Dict[str, int] = {}
        files_modified: Set[str] = set()
        git_commits = 0
        git_pushes = 0
        input_tokens = 0
        output_tokens = 0
        lines_added = 0
        lines_removed = 0
        user_interruptions = 0
        tool_errors = 0
        tool_error_categories: Dict[str, int] = {}
        user_response_times: List[float] = []
        message_hours: List[int] = []
        user_message_timestamps: List[str] = []
        uses_agent = False
        uses_mcp = False
        uses_web_search = False
        uses_web_fetch = False
        user_message_count = 0
        assistant_message_count = 0
        first_prompt = ""

        last_assistant_ts: float | None = None

        for turn in trace.turns:
            # Count messages
            if turn.prompt_text:
                user_message_count += 1
                if not first_prompt:
                    first_prompt = turn.prompt_text[:200]
                # Response time
                if last_assistant_ts is not None:
                    try:
                        user_ts = datetime.fromisoformat(trace.created_at).timestamp()
                    except (ValueError, TypeError):
                        user_ts = None
                # Message hour
                try:
                    ts = datetime.fromisoformat(trace.created_at)
                    message_hours.append(ts.hour)
                    user_message_timestamps.append(ts.isoformat())
                except (ValueError, TypeError):
                    pass

            if turn.final_text:
                assistant_message_count += 1

            # Interruptions
            if turn.turn_status == TurnStatus.INTERRUPTED:
                user_interruptions += 1

            # Tokens
            if turn.token_count:
                output_tokens += turn.token_count

            # Process tool calls
            for tc in turn.tool_calls:
                name = tc.get("name", "unknown")
                tool_counts[name] = tool_counts.get(name, 0) + 1

                # Special tools
                if name == "Agent":
                    uses_agent = True
                elif name.startswith("mcp__"):
                    uses_mcp = True
                elif name == "WebSearch":
                    uses_web_search = True
                elif name == "WebFetch":
                    uses_web_fetch = True

                # Language + file tracking
                params = tc.get("input_params", {})
                file_path = params.get("file_path", "")
                if file_path:
                    ext = os.path.splitext(file_path)[1].lower()
                    lang = EXTENSION_TO_LANGUAGE.get(ext)
                    if lang:
                        languages[lang] = languages.get(lang, 0) + 1
                    if name in ("Edit", "Write"):
                        files_modified.add(file_path)

                # Line changes
                if name == "Write" and "content" in params:
                    content = params["content"]
                    lines_added += content.count("\n") + 1
                elif name == "Edit":
                    old = params.get("old_string", "")
                    new = params.get("new_string", "")
                    old_lines = old.count("\n") + 1 if old else 0
                    new_lines = new.count("\n") + 1 if new else 0
                    lines_added += max(0, new_lines - old_lines)
                    lines_removed += max(0, old_lines - new_lines)

                # Git operations
                if name == "Bash":
                    cmd = params.get("command", "")
                    if "git commit" in cmd:
                        git_commits += 1
                    if "git push" in cmd:
                        git_pushes += 1

                # Error classification
                if tc.get("is_error"):
                    tool_errors += 1
                    error_text = tc.get("error_text", "").lower()
                    category = "Other"
                    for keywords, cat_name in ERROR_CATEGORIES:
                        if any(kw in error_text for kw in keywords):
                            category = cat_name
                            break
                    tool_error_categories[category] = tool_error_categories.get(category, 0) + 1

        # Duration
        try:
            start = datetime.fromisoformat(trace.created_at)
            duration = 0.0  # Will be calculated from turn data if available
        except (ValueError, TypeError):
            start = datetime.now()
            duration = 0.0

        return SessionMeta(
            session_id=trace.session_id,
            tenant_id=trace.tenant_id,
            user_id=trace.user_id,
            project_path="",  # Populated from trace context if available
            start_time=trace.created_at,
            duration_minutes=duration,
            user_message_count=user_message_count,
            assistant_message_count=assistant_message_count,
            tool_counts=tool_counts,
            languages=languages,
            git_commits=git_commits,
            git_pushes=git_pushes,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            first_prompt=first_prompt,
            user_interruptions=user_interruptions,
            user_response_times=user_response_times,
            tool_errors=tool_errors,
            tool_error_categories=tool_error_categories,
            uses_agent=uses_agent,
            uses_mcp=uses_mcp,
            uses_web_search=uses_web_search,
            uses_web_fetch=uses_web_fetch,
            lines_added=lines_added,
            lines_removed=lines_removed,
            files_modified=len(files_modified),
            message_hours=message_hours,
            user_message_timestamps=user_message_timestamps,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/insights/test_extractor.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/insights/extractor.py tests/insights/test_extractor.py
git commit -m "feat(insights): add SessionMetaExtractor with CC-equivalent pure-code extraction"
```

---

### Task 6: InsightsCache

**Files:**
- Create: `src/opencortex/insights/cache.py`
- Test: `tests/insights/test_cache.py`

**Depends on:** Task 3 (types)

- [ ] **Step 1: Write test**

```python
# tests/insights/test_cache.py
"""Tests for InsightsCache (CortexFS-backed)."""
import json
import unittest
from dataclasses import asdict
from unittest.mock import AsyncMock

from opencortex.insights.cache import InsightsCache, _validate_facet
from opencortex.insights.types import SessionMeta, SessionFacet


class TestValidateFacet(unittest.TestCase):
    def test_valid(self):
        data = {
            "session_id": "s1", "underlying_goal": "g",
            "goal_categories": {}, "outcome": "fully_achieved",
            "brief_summary": "summary",
        }
        self.assertTrue(_validate_facet(data))

    def test_missing_field(self):
        data = {"session_id": "s1", "underlying_goal": "g"}
        self.assertFalse(_validate_facet(data))


class TestInsightsCache(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fs = AsyncMock()
        self.cache = InsightsCache(self.fs)

    async def test_put_and_get_meta(self):
        meta = SessionMeta(
            session_id="s1", tenant_id="t1", user_id="u1",
            project_path="", start_time="", duration_minutes=0,
            user_message_count=5, assistant_message_count=5,
            tool_counts={"Read": 3}, languages={"Python": 2},
            git_commits=1, git_pushes=0,
            input_tokens=100, output_tokens=200, first_prompt="hi",
        )
        self.fs.read = AsyncMock(return_value=json.dumps(asdict(meta)))

        await self.cache.put_meta("t1", "u1", "s1", meta)
        self.fs.write.assert_called_once()

        result = await self.cache.get_meta("t1", "u1", "s1")
        self.assertIsNotNone(result)
        self.assertEqual(result.session_id, "s1")

    async def test_get_meta_miss(self):
        self.fs.read = AsyncMock(return_value=None)
        result = await self.cache.get_meta("t1", "u1", "missing")
        self.assertIsNone(result)

    async def test_put_and_get_facet(self):
        facet = SessionFacet(
            session_id="s1", underlying_goal="goal",
            goal_categories={"implement_feature": 1},
            outcome="fully_achieved",
            user_satisfaction_counts={"satisfied": 1},
            claude_helpfulness="very_helpful",
            session_type="single_task",
            brief_summary="summary",
        )
        self.fs.read = AsyncMock(return_value=json.dumps(asdict(facet)))

        await self.cache.put_facet("t1", "u1", "s1", facet)
        self.fs.write.assert_called_once()

        result = await self.cache.get_facet("t1", "u1", "s1")
        self.assertIsNotNone(result)
        self.assertEqual(result.session_id, "s1")
        self.assertEqual(result.goal_categories["implement_feature"], 1)

    async def test_corrupted_facet_deleted(self):
        self.fs.read = AsyncMock(return_value='{"session_id": "s1"}')
        self.fs.delete = AsyncMock()
        result = await self.cache.get_facet("t1", "u1", "s1")
        self.assertIsNone(result)
        self.fs.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/insights/test_cache.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implement cache**

```python
# src/opencortex/insights/cache.py
"""InsightsCache — CortexFS-backed cache for SessionMeta and SessionFacet."""

import json
import logging
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from opencortex.insights.types import SessionMeta, SessionFacet

logger = logging.getLogger(__name__)

REQUIRED_FACET_FIELDS = {
    "session_id", "underlying_goal", "goal_categories",
    "outcome", "brief_summary",
}


def _validate_facet(data: dict) -> bool:
    """Check that a facet dict has all required fields."""
    return REQUIRED_FACET_FIELDS.issubset(data.keys())


class InsightsCache:
    """CortexFS-backed cache for insights data."""

    def __init__(self, cortex_fs: Any):
        self._fs = cortex_fs

    def _meta_uri(self, tid: str, uid: str, session_id: str) -> str:
        return f"opencortex://{tid}/{uid}/insights/cache/meta/{session_id}.json"

    def _facet_uri(self, tid: str, uid: str, session_id: str) -> str:
        return f"opencortex://{tid}/{uid}/insights/cache/facets/{session_id}.json"

    async def get_meta(self, tid: str, uid: str, session_id: str) -> Optional[SessionMeta]:
        uri = self._meta_uri(tid, uid, session_id)
        try:
            content = await self._fs.read(uri)
            if not content:
                return None
            data = json.loads(content)
            return SessionMeta(**data)
        except Exception as e:
            logger.debug(f"Cache miss for meta {session_id}: {e}")
            return None

    async def put_meta(self, tid: str, uid: str, session_id: str, meta: SessionMeta) -> None:
        uri = self._meta_uri(tid, uid, session_id)
        await self._fs.write(uri, json.dumps(asdict(meta)))

    async def get_facet(self, tid: str, uid: str, session_id: str) -> Optional[SessionFacet]:
        uri = self._facet_uri(tid, uid, session_id)
        try:
            content = await self._fs.read(uri)
            if not content:
                return None
            data = json.loads(content)
            if not _validate_facet(data):
                logger.warning(f"Corrupted facet cache for {session_id}, deleting")
                await self._fs.delete(uri)
                return None
            return SessionFacet(**{
                k: v for k, v in data.items()
                if k in SessionFacet.__dataclass_fields__
            })
        except Exception as e:
            logger.debug(f"Cache miss for facet {session_id}: {e}")
            return None

    async def put_facet(self, tid: str, uid: str, session_id: str, facet: SessionFacet) -> None:
        uri = self._facet_uri(tid, uid, session_id)
        await self._fs.write(uri, json.dumps(asdict(facet)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/insights/test_cache.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/insights/cache.py tests/insights/test_cache.py
git commit -m "feat(insights): add CortexFS-backed InsightsCache for meta and facet data"
```

---

### Task 7: Prompts Rewrite

**Files:**
- Modify: `src/opencortex/insights/prompts.py`
- Modify: `tests/insights/test_prompts.py`

- [ ] **Step 1: Write test**

```python
# tests/insights/test_prompts.py
"""Tests for CC-equivalent insights prompts."""
import unittest
from opencortex.insights.prompts import (
    FACET_EXTRACTION_PROMPT,
    CHUNK_SUMMARY_PROMPT,
    PROJECT_AREAS_PROMPT,
    INTERACTION_STYLE_PROMPT,
    WHAT_WORKS_PROMPT,
    FRICTION_ANALYSIS_PROMPT,
    SUGGESTIONS_PROMPT,
    ON_THE_HORIZON_PROMPT,
    FUN_ENDING_PROMPT,
    AT_A_GLANCE_PROMPT,
)


class TestPrompts(unittest.TestCase):
    def test_all_ten_prompts_defined(self):
        prompts = [
            FACET_EXTRACTION_PROMPT, CHUNK_SUMMARY_PROMPT,
            PROJECT_AREAS_PROMPT, INTERACTION_STYLE_PROMPT,
            WHAT_WORKS_PROMPT, FRICTION_ANALYSIS_PROMPT,
            SUGGESTIONS_PROMPT, ON_THE_HORIZON_PROMPT,
            FUN_ENDING_PROMPT, AT_A_GLANCE_PROMPT,
        ]
        for p in prompts:
            self.assertIsInstance(p, str)
            self.assertGreater(len(p), 50)

    def test_facet_has_critical_guidelines(self):
        self.assertIn("CRITICAL GUIDELINES", FACET_EXTRACTION_PROMPT)
        self.assertIn("goal_categories", FACET_EXTRACTION_PROMPT)
        self.assertIn("{transcript}", FACET_EXTRACTION_PROMPT)
        self.assertIn("user_instructions_to_claude", FACET_EXTRACTION_PROMPT)

    def test_chunk_summary_placeholder(self):
        self.assertIn("{chunk}", CHUNK_SUMMARY_PROMPT)

    def test_section_prompts_have_data_context(self):
        for p in [PROJECT_AREAS_PROMPT, INTERACTION_STYLE_PROMPT,
                   WHAT_WORKS_PROMPT, FRICTION_ANALYSIS_PROMPT,
                   SUGGESTIONS_PROMPT, ON_THE_HORIZON_PROMPT,
                   FUN_ENDING_PROMPT]:
            self.assertIn("{data_context}", p)

    def test_at_a_glance_has_section_refs(self):
        self.assertIn("{project_areas_text}", AT_A_GLANCE_PROMPT)
        self.assertIn("{big_wins_text}", AT_A_GLANCE_PROMPT)
        self.assertIn("{friction_text}", AT_A_GLANCE_PROMPT)
        self.assertIn("{features_text}", AT_A_GLANCE_PROMPT)
        self.assertIn("{horizon_text}", AT_A_GLANCE_PROMPT)

    def test_suggestions_has_oc_features_reference(self):
        self.assertIn("OC FEATURES REFERENCE", SUGGESTIONS_PROMPT)
        self.assertIn("Memory Feedback", SUGGESTIONS_PROMPT)
        self.assertIn("Knowledge Pipeline", SUGGESTIONS_PROMPT)
        self.assertIn("Batch Import", SUGGESTIONS_PROMPT)

    def test_interaction_style_prompt_exists(self):
        self.assertIn("interaction style", INTERACTION_STYLE_PROMPT.lower())
        self.assertIn("narrative", INTERACTION_STYLE_PROMPT)

    def test_fun_ending_prompt_exists(self):
        self.assertIn("memorable", FUN_ENDING_PROMPT.lower())
        self.assertIn("headline", FUN_ENDING_PROMPT)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/insights/test_prompts.py -v`
Expected: FAIL (cannot import INTERACTION_STYLE_PROMPT, FUN_ENDING_PROMPT, AT_A_GLANCE_PROMPT)

- [ ] **Step 3: Rewrite prompts.py**

Replace the entire file content with the 10 CC-equivalent prompts from the spec (Section 4.3, 4.5, 5.5). The full content is in the spec at lines 253-844 — copy all prompt constants exactly as specified there.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/insights/test_prompts.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/insights/prompts.py tests/insights/test_prompts.py
git commit -m "feat(insights): rewrite all prompts to CC-equivalent quality (10 total)"
```

---

### Task 8: Agent Pipeline Rewrite

**Files:**
- Modify: `src/opencortex/insights/agent.py`
- Modify: `src/opencortex/insights/collector.py`
- Modify: `tests/insights/test_agent.py`

**Depends on:** Tasks 1-7

This is the largest task. It rewrites the agent to the CC-equivalent 8-phase pipeline with parallel section execution.

- [ ] **Step 1: Write test for the new pipeline**

```python
# tests/insights/test_agent.py
"""Tests for CC-equivalent 8-phase insights pipeline."""
import json
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from opencortex.alpha.types import Turn, Trace
from opencortex.insights.agent import InsightsAgent
from opencortex.insights.types import SessionMeta, SessionFacet, InsightsReport


def _mock_llm():
    """Mock LLM that returns prompt-specific JSON responses."""
    llm = MagicMock()

    def generate_side_effect(prompt, **kwargs):
        if "goal_categories" in prompt and "CRITICAL GUIDELINES" in prompt:
            return json.dumps({
                "underlying_goal": "Test goal",
                "goal_categories": {"implement_feature": 1},
                "outcome": "fully_achieved",
                "user_satisfaction_counts": {"satisfied": 1},
                "claude_helpfulness": "very_helpful",
                "session_type": "single_task",
                "friction_counts": {},
                "friction_detail": "",
                "primary_success": "correct_code_edits",
                "brief_summary": "User implemented feature successfully",
                "user_instructions_to_claude": [],
            })
        if "project areas" in prompt.lower():
            return json.dumps({
                "areas": [{"name": "Backend", "session_count": 5, "description": "API work"}]
            })
        if "interaction style" in prompt.lower():
            return json.dumps({"narrative": "You code fast.", "key_pattern": "Iterative"})
        if "what's working" in prompt.lower() or "impressive" in prompt.lower():
            return json.dumps({
                "intro": "Good work.",
                "impressive_workflows": [{"title": "Fast debugging", "description": "Quick fixes"}],
            })
        if "friction" in prompt.lower() and "categories" in prompt.lower():
            return json.dumps({
                "intro": "Some issues.",
                "categories": [{"category": "Wrong approach", "description": "Missed target", "examples": ["ex1"]}],
            })
        if "OC FEATURES REFERENCE" in prompt:
            return json.dumps({
                "features_to_try": [{"feature": "Memory Feedback", "one_liner": "RL", "why_for_you": "helps", "example_code": "feedback(uri, 1)"}],
                "usage_patterns": [{"title": "Batch imports", "suggestion": "Use batch_store", "detail": "d", "copyable_prompt": "p"}],
            })
        if "future opportunities" in prompt.lower() or "horizon" in prompt.lower():
            return json.dumps({
                "intro": "Big things ahead.",
                "opportunities": [{"title": "Auto-testing", "whats_possible": "Autonomous", "how_to_try": "Try it", "copyable_prompt": "p"}],
            })
        if "memorable" in prompt.lower():
            return json.dumps({"headline": "That time it worked first try", "detail": "Session 3"})
        if "At a Glance" in prompt:
            return json.dumps({
                "whats_working": "Good patterns",
                "whats_hindering": "Some friction",
                "quick_wins": "Try memory feedback",
                "ambitious_workflows": "Autonomous testing",
            })
        return json.dumps({})

    llm.generate = generate_side_effect

    async def async_gen(prompt, **kwargs):
        return generate_side_effect(prompt, **kwargs)

    llm.generate_async = async_gen
    return llm


def _mock_trace(session_id="s1"):
    return Trace(
        trace_id="tr1", session_id=session_id,
        tenant_id="t1", user_id="u1", source="claude_code",
        turns=[
            Turn(turn_id="1", prompt_text="Fix the bug", final_text="Done",
                 tool_calls=[{"name": "Read", "input_params": {"file_path": "/app.py"}}]),
            Turn(turn_id="2", prompt_text="Now add tests", final_text="Tests added",
                 tool_calls=[{"name": "Edit", "input_params": {"file_path": "/test.py"}}]),
        ],
        created_at="2026-03-31T10:00:00+00:00",
    )


class TestInsightsAgentPipeline(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.llm = _mock_llm()
        self.cache = AsyncMock()
        self.cache.get_meta = AsyncMock(return_value=None)
        self.cache.put_meta = AsyncMock()
        self.cache.get_facet = AsyncMock(return_value=None)
        self.cache.put_facet = AsyncMock()
        self.collector = AsyncMock()
        self.collector.fetch_traces = AsyncMock(return_value=[_mock_trace()])

    async def test_full_pipeline_returns_report(self):
        agent = InsightsAgent(
            llm=self.llm, collector=self.collector, cache=self.cache,
        )
        report = await agent.analyze_async("t1", "u1", date(2026, 3, 1), date(2026, 3, 31))
        self.assertIsInstance(report, InsightsReport)
        self.assertEqual(report.tenant_id, "t1")
        self.assertGreater(report.total_sessions, 0)

    async def test_at_a_glance_is_dict(self):
        agent = InsightsAgent(
            llm=self.llm, collector=self.collector, cache=self.cache,
        )
        report = await agent.analyze_async("t1", "u1", date(2026, 3, 1), date(2026, 3, 31))
        self.assertIsInstance(report.at_a_glance, dict)
        self.assertIn("whats_working", report.at_a_glance)

    async def test_empty_traces(self):
        self.collector.fetch_traces = AsyncMock(return_value=[])
        agent = InsightsAgent(
            llm=self.llm, collector=self.collector, cache=self.cache,
        )
        report = await agent.analyze_async("t1", "u1", date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual(report.total_sessions, 0)

    async def test_cache_used_for_meta(self):
        cached_meta = SessionMeta(
            session_id="s1", tenant_id="t1", user_id="u1",
            project_path="", start_time="2026-03-31T10:00:00",
            duration_minutes=10, user_message_count=5,
            assistant_message_count=5, tool_counts={"Read": 3},
            languages={"Python": 2}, git_commits=1, git_pushes=0,
            input_tokens=100, output_tokens=200, first_prompt="hi",
        )
        self.cache.get_meta = AsyncMock(return_value=cached_meta)
        agent = InsightsAgent(
            llm=self.llm, collector=self.collector, cache=self.cache,
        )
        report = await agent.analyze_async("t1", "u1", date(2026, 3, 1), date(2026, 3, 31))
        self.cache.put_meta.assert_not_called()
        self.assertIsInstance(report, InsightsReport)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/insights/test_agent.py -v`
Expected: FAIL (imports fail due to new constructor signature)

- [ ] **Step 3: Rewrite agent.py**

Rewrite `src/opencortex/insights/agent.py` implementing the 8-phase pipeline as specified in the spec Section 7. Key changes:
- Constructor takes `llm`, `collector`, `cache` (InsightsCache)
- `analyze_async()` follows the 8 phases: load traces → extract meta (with cache) → dedup → filter → facet extraction (with cache + concurrency) → filter warmup → aggregate → parallel sections + serial at_a_glance
- Uses `asyncio.gather` for parallel section generation
- Builds `data_context` via `build_data_context()`
- Uses `generate_parallel_insights()` for 7+1 execution pattern
- Assembles enriched `InsightsReport` with all section data

Also simplify `src/opencortex/insights/collector.py` to only expose `fetch_traces()` — the aggregation logic moves into the agent.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/insights/test_agent.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/insights/agent.py src/opencortex/insights/collector.py tests/insights/test_agent.py
git commit -m "feat(insights): rewrite agent to CC-equivalent 8-phase pipeline with parallel sections"
```

---

### Task 9: Report Serialization + API Update

**Files:**
- Modify: `src/opencortex/insights/report.py`
- Modify: `src/opencortex/insights/api.py`
- Modify: `tests/insights/test_report.py`

**Depends on:** Task 8

- [ ] **Step 1: Write test for enriched serialization**

```python
# tests/insights/test_report.py (add to existing)
"""Tests for enriched report serialization."""
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from opencortex.insights.report import ReportManager
from opencortex.insights.types import InsightsReport, SessionFacet


class TestEnrichedSerialization(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fs = AsyncMock()
        self.manager = ReportManager(self.fs)

    async def test_serialize_includes_enriched_fields(self):
        report = InsightsReport(
            tenant_id="t1", user_id="u1",
            report_period="2026-03-01 - 2026-03-31",
            generated_at=datetime(2026, 3, 31, 12, 0),
            total_sessions=5, total_messages=50,
            total_duration_hours=3.0,
            at_a_glance={
                "whats_working": "Good patterns",
                "whats_hindering": "Some friction",
                "quick_wins": "Try feedback",
                "ambitious_workflows": "Autonomous testing",
            },
            interaction_style={"narrative": "Fast coder", "key_pattern": "Iterative"},
            fun_ending={"headline": "It worked!", "detail": "Session 3"},
            aggregated={"tool_counts": {"Read": 10}, "languages": {"Python": 5}},
        )
        json_str = self.manager._serialize_report_to_json(report)
        data = json.loads(json_str)

        self.assertIsInstance(data["at_a_glance"], dict)
        self.assertEqual(data["at_a_glance"]["whats_working"], "Good patterns")
        self.assertEqual(data["interaction_style"]["narrative"], "Fast coder")
        self.assertEqual(data["fun_ending"]["headline"], "It worked!")
        self.assertIn("tool_counts", data["aggregated"])

    async def test_serialize_backward_compat(self):
        """Report with no enriched fields still serializes."""
        report = InsightsReport(
            tenant_id="t1", user_id="u1",
            report_period="2026-03-01 - 2026-03-31",
            generated_at=datetime(2026, 3, 31),
            total_sessions=0, total_messages=0,
            total_duration_hours=0,
        )
        json_str = self.manager._serialize_report_to_json(report)
        data = json.loads(json_str)
        self.assertEqual(data["at_a_glance"], {})
        self.assertIsNone(data["interaction_style"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/insights/test_report.py::TestEnrichedSerialization -v`
Expected: FAIL (serialization doesn't include enriched fields)

- [ ] **Step 3: Update report.py serialization**

Update `_serialize_report_to_json()` in `report.py` to include all new enriched fields:

```python
def _serialize_report_to_json(self, report: InsightsReport) -> str:
    data = {
        "tenant_id": report.tenant_id,
        "user_id": report.user_id,
        "report_period": report.report_period,
        "generated_at": report.generated_at.isoformat(),
        "total_sessions": report.total_sessions,
        "total_messages": report.total_messages,
        "total_duration_hours": report.total_duration_hours,
        "at_a_glance": report.at_a_glance,
        "interaction_style": report.interaction_style,
        "what_works_detail": report.what_works_detail,
        "friction_detail": report.friction_detail,
        "suggestions_detail": report.suggestions_detail,
        "on_the_horizon_detail": report.on_the_horizon_detail,
        "fun_ending": report.fun_ending,
        "aggregated": report.aggregated,
        "cache_hits": report.cache_hits,
        "llm_calls": report.llm_calls,
        # Legacy fields for backward compat
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
    return json.dumps(data, indent=2, default=str)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/insights/test_report.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/insights/report.py src/opencortex/insights/api.py tests/insights/test_report.py
git commit -m "feat(insights): update report serialization for enriched CC-equivalent data"
```

---

### Task 10: Full Integration Test

**Files:**
- Modify: `tests/insights/test_agent.py` (add integration test)

- [ ] **Step 1: Run full insights test suite**

Run: `uv run python3 -m pytest tests/insights/ -v`
Expected: All tests PASS across all test files

- [ ] **Step 2: Verify imports are clean**

Run: `uv run python3 -c "from opencortex.insights.constants import *; from opencortex.insights.labels import *; from opencortex.insights.types import *; from opencortex.insights.extractor import *; from opencortex.insights.cache import *; from opencortex.insights.multi_clauding import *; from opencortex.insights.prompts import *; print('All imports OK')"`
Expected: "All imports OK"

- [ ] **Step 3: Commit final state**

```bash
git add -A
git commit -m "test(insights): verify full backend pipeline integration"
```
