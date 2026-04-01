"""Tests for InsightsAgent - CC-equivalent 8-phase pipeline."""

import asyncio
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from opencortex.alpha.types import Trace, Turn, TurnStatus
from opencortex.insights.agent import (
    InsightsAgent,
    aggregate_data,
    build_data_context,
    deduplicate_sessions,
    filter_substantive,
    filter_warmup_only,
)
from opencortex.insights.types import (
    AggregatedData,
    InsightsReport,
    SessionFacet,
    SessionMeta,
)


# ---------------------------------------------------------------------------
# Mock LLM: returns prompt-specific JSON based on keywords
# ---------------------------------------------------------------------------

class MockLLM:
    """Mock LLM that returns structured JSON based on prompt keywords."""

    def __init__(self):
        self.call_count = 0

    def generate(self, prompt: str, **kwargs) -> str:
        self.call_count += 1
        p = prompt.lower()

        # AT_A_GLANCE must be checked first -- its prompt contains other
        # section keywords like "Project Areas", "Friction", etc.
        if "at a glance" in p and "## project areas" in p:
            return json.dumps({
                "whats_working": "Strong iterative debugging workflow.",
                "whats_hindering": "Config path confusion.",
                "quick_wins": "Try memory feedback to refine recall.",
                "ambitious_workflows": "Explore parallel agent execution.",
            })

        if "analyze this claude code session" in p or "extract structured facets" in p:
            return json.dumps({
                "underlying_goal": "Fix authentication bug",
                "goal_categories": {"debug_investigate": 1, "fix_bug": 1},
                "outcome": "fully_achieved",
                "user_satisfaction_counts": {"satisfied": 2},
                "claude_helpfulness": "very_helpful",
                "session_type": "single_task",
                "friction_counts": {"misunderstood_request": 1},
                "friction_detail": "Misread config path once",
                "primary_success": "correct_code_edits",
                "brief_summary": "Fixed auth token expiry by updating config",
                "user_instructions_to_claude": ["be concise"],
            })

        if "summarize this portion" in p:
            return "Session involved debugging auth. Token expiry fixed."

        if "identify project areas" in p:
            return json.dumps({
                "areas": [
                    {"name": "Authentication", "session_count": 3,
                     "description": "Auth module improvements."},
                ]
            })

        if "interaction style" in p or "describe the user" in p:
            return json.dumps({
                "narrative": "You iterate quickly with short prompts.",
                "key_pattern": "Rapid iteration with minimal context",
            })

        if "what's working well" in p or "identify what" in p:
            return json.dumps({
                "intro": "Strong debugging workflows.",
                "impressive_workflows": [
                    {"title": "Quick Debugging", "description": "Fast isolation."},
                ],
            })

        if "identify friction points" in p:
            return json.dumps({
                "intro": "Minor friction around config.",
                "categories": [
                    {"category": "Config Issues", "description": "Misread paths.",
                     "examples": ["wrong config path"]},
                ],
            })

        if "suggest improvements" in p or "oc features reference" in p:
            return json.dumps({
                "features_to_try": [
                    {"feature": "Memory Feedback", "one_liner": "Reinforce useful memories",
                     "why_for_you": "Improve recall", "example_code": "feedback(uri, +1)"},
                ],
                "usage_patterns": [
                    {"title": "Batch Import", "suggestion": "Import docs",
                     "detail": "Speed up onboarding", "copyable_prompt": "batch_store(...)"},
                ],
            })

        if "future opportunities" in p or "identify future" in p:
            return json.dumps({
                "intro": "AI workflows evolving.",
                "opportunities": [
                    {"title": "Parallel Agents", "whats_possible": "Run multiple tasks.",
                     "how_to_try": "Use Agent tool.", "copyable_prompt": "try parallel agents"},
                ],
            })

        if "memorable moment" in p or "find a memorable" in p:
            return json.dumps({
                "headline": "That time you fixed the bug in 30 seconds",
                "detail": "Fastest debug ever on Tuesday",
            })

        # Fallback
        return json.dumps({"result": "ok"})


# ---------------------------------------------------------------------------
# Mock Collector: returns pre-configured traces
# ---------------------------------------------------------------------------

class MockCollector:
    """Mock InsightsCollector that returns configured traces."""

    def __init__(self, traces: Optional[List[Trace]] = None):
        self.traces = traces or []

    async def fetch_traces(
        self, tenant_id: str, user_id: str, start_date: date, end_date: date,
    ) -> List[Trace]:
        return self.traces


# ---------------------------------------------------------------------------
# Mock Cache
# ---------------------------------------------------------------------------

class MockCache:
    """Mock InsightsCache with get/put tracking."""

    def __init__(self):
        self._meta: Dict[str, SessionMeta] = {}
        self._facet: Dict[str, SessionFacet] = {}
        self.meta_hits = 0
        self.facet_hits = 0

    async def get_meta(self, tid, uid, session_id) -> Optional[SessionMeta]:
        key = f"{tid}/{uid}/{session_id}"
        if key in self._meta:
            self.meta_hits += 1
            return self._meta[key]
        return None

    async def put_meta(self, tid, uid, session_id, meta):
        key = f"{tid}/{uid}/{session_id}"
        self._meta[key] = meta

    async def get_facet(self, tid, uid, session_id) -> Optional[SessionFacet]:
        key = f"{tid}/{uid}/{session_id}"
        if key in self._facet:
            self.facet_hits += 1
            return self._facet[key]
        return None

    async def put_facet(self, tid, uid, session_id, facet):
        key = f"{tid}/{uid}/{session_id}"
        self._facet[key] = facet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_trace(
    session_id: str = "sess-1",
    user_messages: int = 5,
    tenant_id: str = "t1",
    user_id: str = "u1",
    created_at: Optional[str] = None,
) -> Trace:
    """Build a Trace with the specified number of user/assistant turn pairs."""
    turns = []
    now_str = created_at or datetime.now(timezone.utc).isoformat()
    for i in range(user_messages):
        turns.append(Turn(
            turn_id=f"turn-{i*2}",
            prompt_text=f"User message {i}",
            tool_calls=[{"name": "Edit", "input_params": {"file_path": "/src/main.py"}}]
            if i == 0 else [],
        ))
        turns.append(Turn(
            turn_id=f"turn-{i*2+1}",
            final_text=f"Assistant response {i}",
        ))
    return Trace(
        trace_id=f"trace-{session_id}",
        session_id=session_id,
        tenant_id=tenant_id,
        user_id=user_id,
        source="claude_code",
        turns=turns,
        created_at=now_str,
    )


def make_meta(session_id: str = "sess-1", user_msgs: int = 5, duration: float = 10.0) -> SessionMeta:
    return SessionMeta(
        session_id=session_id,
        tenant_id="t1",
        user_id="u1",
        project_path="/project",
        start_time=datetime.now(timezone.utc).isoformat(),
        duration_minutes=duration,
        user_message_count=user_msgs,
        assistant_message_count=user_msgs,
        tool_counts={"Edit": 2},
        languages={"Python": 3},
        git_commits=1,
        git_pushes=0,
        input_tokens=1000,
        output_tokens=800,
        first_prompt="Fix the auth bug",
    )


def make_facet(session_id: str = "sess-1", goals: Optional[Dict[str, int]] = None) -> SessionFacet:
    return SessionFacet(
        session_id=session_id,
        underlying_goal="Fix bug",
        goal_categories=goals or {"fix_bug": 1},
        outcome="fully_achieved",
        user_satisfaction_counts={"satisfied": 1},
        claude_helpfulness="very_helpful",
        session_type="single_task",
        brief_summary="Fixed the bug",
    )


# ===========================================================================
# Tests
# ===========================================================================

class TestDeduplicateSessions(unittest.TestCase):

    def test_keeps_highest_message_count(self):
        t1, m1 = make_trace("s1"), make_meta("s1", user_msgs=3)
        t2, m2 = make_trace("s1"), make_meta("s1", user_msgs=10)
        result = deduplicate_sessions([(t1, m1), (t2, m2)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1].user_message_count, 10)

    def test_tiebreak_by_duration(self):
        t1, m1 = make_trace("s1"), make_meta("s1", user_msgs=5, duration=2.0)
        t2, m2 = make_trace("s1"), make_meta("s1", user_msgs=5, duration=20.0)
        result = deduplicate_sessions([(t1, m1), (t2, m2)])
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0][1].duration_minutes, 20.0)

    def test_different_sessions_kept(self):
        entries = [
            (make_trace("s1"), make_meta("s1")),
            (make_trace("s2"), make_meta("s2")),
        ]
        result = deduplicate_sessions(entries)
        self.assertEqual(len(result), 2)


class TestFilterSubstantive(unittest.TestCase):

    def test_filters_low_message_count(self):
        entries = [
            (make_trace("s1"), make_meta("s1", user_msgs=1, duration=10.0)),
            (make_trace("s2"), make_meta("s2", user_msgs=5, duration=10.0)),
        ]
        result = filter_substantive(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1].session_id, "s2")

    def test_filters_short_duration(self):
        entries = [
            (make_trace("s1"), make_meta("s1", user_msgs=5, duration=0.5)),
            (make_trace("s2"), make_meta("s2", user_msgs=5, duration=10.0)),
        ]
        result = filter_substantive(entries)
        self.assertEqual(len(result), 1)


class TestFilterWarmupOnly(unittest.TestCase):

    def test_removes_warmup_only(self):
        entries = [
            (make_trace("s1"), make_meta("s1")),
            (make_trace("s2"), make_meta("s2")),
        ]
        facets = {
            "s1": make_facet("s1", goals={"warmup_minimal": 1}),
            "s2": make_facet("s2", goals={"fix_bug": 1}),
        }
        result, cleaned_facets = filter_warmup_only(entries, facets)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1].session_id, "s2")
        # Warmup-only session s1 should be removed from facets
        self.assertNotIn("s1", cleaned_facets)
        self.assertIn("s2", cleaned_facets)

    def test_keeps_mixed_goals_with_warmup(self):
        entries = [(make_trace("s1"), make_meta("s1"))]
        facets = {
            "s1": make_facet("s1", goals={"warmup_minimal": 1, "fix_bug": 1}),
        }
        result, cleaned_facets = filter_warmup_only(entries, facets)
        self.assertEqual(len(result), 1)
        self.assertIn("s1", cleaned_facets)

    def test_keeps_sessions_without_facet(self):
        entries = [(make_trace("s1"), make_meta("s1"))]
        result, cleaned_facets = filter_warmup_only(entries, {})
        self.assertEqual(len(result), 1)


class TestAggregateData(unittest.TestCase):

    def test_computes_totals(self):
        metas = [make_meta("s1"), make_meta("s2")]
        facets = {
            "s1": make_facet("s1"),
            "s2": make_facet("s2"),
        }
        agg = aggregate_data(metas, facets, date.today() - timedelta(days=7), date.today(), 10)
        self.assertIsInstance(agg, AggregatedData)
        self.assertEqual(agg.total_sessions, 2)
        self.assertEqual(agg.total_sessions_scanned, 10)
        self.assertEqual(agg.sessions_with_facets, 2)
        self.assertGreater(agg.total_messages, 0)

    def test_merges_tool_counts(self):
        m1 = make_meta("s1")
        m1.tool_counts = {"Edit": 3, "Read": 1}
        m2 = make_meta("s2")
        m2.tool_counts = {"Edit": 2, "Write": 4}
        agg = aggregate_data([m1, m2], {}, date.today(), date.today(), 2)
        self.assertEqual(agg.tool_counts["Edit"], 5)
        self.assertEqual(agg.tool_counts["Read"], 1)
        self.assertEqual(agg.tool_counts["Write"], 4)

    def test_computes_response_time_stats(self):
        m1 = make_meta("s1")
        m1.user_response_times = [10.0, 20.0, 30.0]
        agg = aggregate_data([m1], {}, date.today(), date.today(), 1)
        self.assertAlmostEqual(agg.median_response_time, 20.0)
        self.assertAlmostEqual(agg.avg_response_time, 20.0)

    def test_counts_days_active(self):
        m1 = make_meta("s1")
        m1.start_time = "2026-03-25T10:00:00+00:00"
        m2 = make_meta("s2")
        m2.start_time = "2026-03-26T14:00:00+00:00"
        m3 = make_meta("s3")
        m3.start_time = "2026-03-25T16:00:00+00:00"  # same day as m1
        agg = aggregate_data([m1, m2, m3], {}, date(2026, 3, 25), date(2026, 3, 26), 3)
        self.assertEqual(agg.days_active, 2)


class TestInsightsAgentPipeline(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.llm = MockLLM()
        self.tenant_id = "t1"
        self.user_id = "u1"
        self.start_date = date.today() - timedelta(days=7)
        self.end_date = date.today()

    async def test_full_pipeline_returns_report(self):
        """Full pipeline with 2 traces produces a complete report."""
        traces = [
            make_trace("sess-1", user_messages=5),
            make_trace("sess-2", user_messages=3),
        ]
        collector = MockCollector(traces)
        cache = MockCache()
        agent = InsightsAgent(llm=self.llm, collector=collector, cache=cache)

        report = await agent.analyze_async(
            self.tenant_id, self.user_id, self.start_date, self.end_date,
        )

        self.assertIsInstance(report, InsightsReport)
        self.assertEqual(report.tenant_id, self.tenant_id)
        self.assertEqual(report.user_id, self.user_id)
        self.assertGreater(report.total_sessions, 0)
        self.assertGreater(report.llm_calls, 0)
        # Sections populated
        self.assertIsNotNone(report.project_areas)
        self.assertIsNotNone(report.interaction_style)
        self.assertIsNotNone(report.fun_ending)

    async def test_at_a_glance_is_dict(self):
        """at_a_glance should be a dict with expected keys."""
        traces = [make_trace("sess-1", user_messages=5)]
        collector = MockCollector(traces)
        agent = InsightsAgent(llm=self.llm, collector=collector)

        report = await agent.analyze_async(
            self.tenant_id, self.user_id, self.start_date, self.end_date,
        )

        self.assertIsInstance(report.at_a_glance, dict)
        self.assertIn("whats_working", report.at_a_glance)

    async def test_empty_traces(self):
        """Empty trace list returns empty report with zero sessions."""
        collector = MockCollector([])
        agent = InsightsAgent(llm=self.llm, collector=collector)

        report = await agent.analyze_async(
            self.tenant_id, self.user_id, self.start_date, self.end_date,
        )

        self.assertIsInstance(report, InsightsReport)
        self.assertEqual(report.total_sessions, 0)
        self.assertEqual(report.total_messages, 0)
        self.assertEqual(report.llm_calls, 0)

    async def test_cache_used_for_meta(self):
        """Cache should be hit on second run for meta extraction."""
        traces = [make_trace("sess-1", user_messages=5)]
        collector = MockCollector(traces)
        cache = MockCache()
        agent = InsightsAgent(llm=self.llm, collector=collector, cache=cache)

        # First run: cache miss, stores meta
        await agent.analyze_async(
            self.tenant_id, self.user_id, self.start_date, self.end_date,
        )
        self.assertEqual(cache.meta_hits, 0)

        # Second run: cache hit
        report = await agent.analyze_async(
            self.tenant_id, self.user_id, self.start_date, self.end_date,
        )
        self.assertGreater(cache.meta_hits, 0)
        self.assertGreater(report.cache_hits, 0)

    async def test_cache_used_for_facets(self):
        """Cache should be hit on second run for facet extraction."""
        traces = [make_trace("sess-1", user_messages=5)]
        collector = MockCollector(traces)
        cache = MockCache()
        agent = InsightsAgent(llm=self.llm, collector=collector, cache=cache)

        # First run: stores facets
        await agent.analyze_async(
            self.tenant_id, self.user_id, self.start_date, self.end_date,
        )
        first_llm_calls = self.llm.call_count

        # Second run: facet cache hit
        self.llm.call_count = 0
        report = await agent.analyze_async(
            self.tenant_id, self.user_id, self.start_date, self.end_date,
        )
        self.assertGreater(cache.facet_hits, 0)

    async def test_aggregated_data_in_report(self):
        """Report should have aggregated data dict with expected keys."""
        traces = [make_trace("sess-1", user_messages=5)]
        collector = MockCollector(traces)
        agent = InsightsAgent(llm=self.llm, collector=collector)

        report = await agent.analyze_async(
            self.tenant_id, self.user_id, self.start_date, self.end_date,
        )

        self.assertIsNotNone(report.aggregated)
        self.assertIn("total_sessions", report.aggregated)
        self.assertIn("tool_counts", report.aggregated)
        self.assertIn("multi_clauding", report.aggregated)

    async def test_all_traces_below_threshold_returns_empty(self):
        """If all traces have < MIN_USER_MESSAGES, return empty report."""
        traces = [make_trace("sess-1", user_messages=1)]
        collector = MockCollector(traces)
        agent = InsightsAgent(llm=self.llm, collector=collector)

        report = await agent.analyze_async(
            self.tenant_id, self.user_id, self.start_date, self.end_date,
        )

        self.assertEqual(report.total_sessions, 0)


if __name__ == "__main__":
    unittest.main()
