"""InsightsAgent - LLM-powered analysis pipeline with 7 stages."""

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from opencortex.insights.types import SessionFacet, InsightsReport
from opencortex.insights import prompts

logger = logging.getLogger(__name__)

WARMUP_MESSAGE_THRESHOLD = 3
MAX_TOKENS_PER_CHUNK = 3000


class InsightsAgent:
    """LLM-powered insights analysis with 7-stage pipeline."""

    def __init__(self, llm: Any, collector: Any, max_concurrent: int = 5):
        """
        Initialize InsightsAgent.

        Args:
            llm: LLM instance for analysis
            collector: InsightsCollector for session data
            max_concurrent: Max concurrent LLM calls
        """
        self._llm = llm
        self._collector = collector
        self._semaphore = asyncio.Semaphore(max_concurrent)

    def analyze(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> InsightsReport:
        """
        Synchronous wrapper for analyze_async.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
            start_date: Analysis start date
            end_date: Analysis end date

        Returns:
            InsightsReport with complete analysis
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                self.analyze_async(tenant_id, user_id, start_date, end_date)
            )
        finally:
            loop.close()

    async def analyze_async(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> InsightsReport:
        """
        Main entry point - orchestrates 7-stage analysis pipeline.

        Stages:
        1. Session Facet Extraction (per-session analysis)
        2. Warmup Session Filtering
        3. Aggregated Metrics Generation
        4. Project Areas Analysis
        5. What Works Recognition
        6. Friction Analysis
        7. At-a-Glance Summary

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
            start_date: Analysis start date
            end_date: Analysis end date

        Returns:
            InsightsReport with complete analysis
        """
        llm_calls = 0

        activity_window = await self._collector.collect_user_sessions_async(
            tenant_id, user_id, start_date, end_date
        )

        sessions = activity_window.get("sessions", 0)
        total_messages = activity_window.get("total_messages", 0)
        total_tokens = activity_window.get("total_tokens", 0)
        duration_hours = total_tokens / 750 if total_tokens else 0

        report = InsightsReport(
            tenant_id=tenant_id,
            user_id=user_id,
            report_period=f"{start_date} to {end_date}",
            generated_at=datetime.now(),
            total_sessions=sessions,
            total_messages=total_messages,
            total_duration_hours=duration_hours,
        )

        if sessions == 0:
            logger.info(f"No sessions found for {user_id}")
            return report

        session_list = getattr(self._collector, "sessions", [])
        if not session_list:
            return report

        facets = self._extract_session_facets(session_list)
        llm_calls += len(session_list)

        filtered_facets = self._filter_warmup_sessions(session_list, facets)

        aggregated = self._aggregate_facets(filtered_facets)

        sessions_summary = self._build_sessions_summary(session_list)

        project_areas_data = self._generate_project_areas(sessions_summary)
        llm_calls += 1

        what_works_data = self._generate_what_works(sessions_summary)
        llm_calls += 1

        friction_data = self._generate_friction_analysis(sessions_summary)
        llm_calls += 1

        findings = {
            "what_works": what_works_data.get("successful_patterns", []),
            "friction": friction_data.get("blockers", []),
        }

        suggestions = self._generate_suggestions(findings)
        llm_calls += 1

        insights_data = {
            "total_sessions": report.total_sessions,
            "total_messages": report.total_messages,
            "what_works": what_works_data.get("successful_patterns", []),
            "friction": friction_data.get("blockers", []),
            "project_areas": project_areas_data.get("areas", []),
        }

        at_a_glance_str = self._generate_at_a_glance(insights_data)
        llm_calls += 1

        on_the_horizon = await self._generate_on_the_horizon(sessions_summary)
        llm_calls += 1

        report.session_facets = filtered_facets
        report.project_areas = {a: 1 for a in project_areas_data.get("areas", [])}
        report.what_works = what_works_data.get("successful_patterns", [])
        report.friction_analysis = {f: 1 for f in friction_data.get("blockers", [])}
        report.suggestions = suggestions
        report.on_the_horizon = on_the_horizon
        report.at_a_glance = at_a_glance_str
        report.llm_calls = llm_calls

        return report

    def _extract_session_facets(
        self, sessions: List[Dict[str, Any]]
    ) -> List[SessionFacet]:
        """
        Stage 1: Extract structured facets from each session.

        Args:
            sessions: List of session records

        Returns:
            List of SessionFacet objects
        """
        facets = []
        for session in sessions:
            transcript = session.get("transcript", "")
            if not transcript:
                continue

            prompt = prompts.FACET_EXTRACTION_PROMPT.format(
                session_transcript=transcript
            )

            try:
                response = self._llm.generate(prompt)
                data = json.loads(response)

                facet = SessionFacet(
                    session_id=session.get("session_id", "unknown"),
                    underlying_goal=data.get("underlying_goal", "Unknown"),
                    brief_summary=data.get("brief_summary", ""),
                    goal_categories=data.get("goal_categories", []),
                    outcome=data.get("outcome", "unclear_from_transcript"),
                    user_satisfaction_counts=data.get("user_satisfaction_counts", {}),
                    claude_helpfulness=float(data.get("claude_helpfulness", 0.5)),
                    session_type=data.get("session_type", "unknown"),
                    friction_counts=data.get("friction_counts", {}),
                    friction_detail=data.get("friction_detail", []),
                    primary_success=data.get("primary_success"),
                )
                facets.append(facet)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to extract facets from session: {e}")
                continue

        return facets

    def _chunk_summarize(self, transcript: str) -> str:
        """
        Chunk and summarize long transcripts.

        Args:
            transcript: Full session transcript

        Returns:
            Summarized content
        """
        if len(transcript) < MAX_TOKENS_PER_CHUNK:
            return transcript

        words = transcript.split()
        chunk_size = MAX_TOKENS_PER_CHUNK // 4

        chunks = []
        for i in range(0, len(words), chunk_size):
            chunk = " ".join(words[i : i + chunk_size])
            chunks.append(chunk)

        summaries = []
        for chunk in chunks:
            prompt = prompts.CHUNK_SUMMARY_PROMPT.format(chunk_content=chunk)
            try:
                summary = self._llm.generate(prompt)
                summaries.append(summary)
            except Exception as e:
                logger.warning(f"Chunk summarization failed: {e}")
                summaries.append(chunk[:200])

        return " ".join(summaries)

    def _filter_warmup_sessions(
        self,
        sessions: List[Dict[str, Any]],
        facets: List[SessionFacet],
    ) -> List[SessionFacet]:
        """
        Stage 2: Filter out warmup-only sessions.

        Args:
            sessions: Session records
            facets: Session facets

        Returns:
            Filtered facets with warmup sessions removed
        """
        filtered = []
        for session, facet in zip(sessions, facets):
            message_count = session.get("message_count", 0)
            if message_count >= WARMUP_MESSAGE_THRESHOLD:
                filtered.append(facet)

        return filtered

    def _aggregate_facets(self, facets: List[SessionFacet]) -> Dict[str, Any]:
        """
        Stage 3: Aggregate facets into metrics.

        Args:
            facets: Session facets

        Returns:
            Aggregated metrics dictionary
        """
        if not facets:
            return {
                "total_sessions": 0,
                "avg_helpfulness": 0.0,
                "outcome_distribution": {},
            }

        total_helpfulness = sum(f.claude_helpfulness for f in facets)
        avg_helpfulness = total_helpfulness / len(facets) if facets else 0.0

        outcome_counts = {}
        for facet in facets:
            outcome = facet.outcome
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

        return {
            "total_sessions": len(facets),
            "avg_helpfulness": avg_helpfulness,
            "outcome_distribution": outcome_counts,
        }

    def _build_sessions_summary(self, sessions: List[Dict[str, Any]]) -> str:
        """Build a combined summary of all sessions."""
        summaries = []
        for session in sessions[:10]:
            transcript = session.get("transcript", "")
            if transcript:
                summary = self._chunk_summarize(transcript[:500])
                summaries.append(summary)

        return "\n".join(summaries)

    def _generate_project_areas(self, sessions_summary: str) -> Dict[str, Any]:
        """
        Stage 4: Analyze project areas.

        Args:
            sessions_summary: Combined session summary

        Returns:
            Project areas analysis
        """
        prompt = prompts.PROJECT_AREAS_PROMPT.format(sessions_summary=sessions_summary)

        try:
            response = self._llm.generate(prompt)
            return json.loads(response)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Project areas generation failed: {e}")
            return {"areas": [], "focus_distribution": {}}

    def _generate_what_works(self, session_data: str) -> Dict[str, Any]:
        """
        Stage 5: Recognize what works well.

        Args:
            session_data: Session data summary

        Returns:
            What works analysis
        """
        prompt = prompts.WHAT_WORKS_PROMPT.format(session_data=session_data)

        try:
            response = self._llm.generate(prompt)
            return json.loads(response)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"What works generation failed: {e}")
            return {
                "successful_patterns": [],
                "effective_tools": [],
                "workflow_strengths": [],
            }

    def _generate_friction_analysis(self, session_data: str) -> Dict[str, Any]:
        """
        Stage 6: Analyze friction points.

        Args:
            session_data: Session data summary

        Returns:
            Friction analysis
        """
        prompt = prompts.FRICTION_ANALYSIS_PROMPT.format(session_data=session_data)

        try:
            response = self._llm.generate(prompt)
            return json.loads(response)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Friction analysis generation failed: {e}")
            return {
                "blockers": [],
                "repeated_issues": [],
                "inefficient_processes": [],
            }

    def _generate_suggestions(self, findings: Dict[str, List[str]]) -> List[str]:
        """Generate actionable suggestions."""
        prompt = prompts.SUGGESTIONS_PROMPT.format(findings=json.dumps(findings))

        try:
            response = self._llm.generate(prompt)
            data = json.loads(response)
            suggestions = []
            for key in [
                "quick_wins",
                "process_improvements",
                "tool_recommendations",
            ]:
                suggestions.extend(data.get(key, []))
            return suggestions[:5]
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Suggestions generation failed: {e}")
            return []

    def _generate_at_a_glance(self, insights_data: Dict[str, Any]) -> str:
        """
        Stage 7: Generate at-a-glance summary.

        Args:
            insights_data: Complete insights data

        Returns:
            At-a-glance summary string
        """
        prompt = prompts.AT_A_GLANCE_PROMPT.format(
            insights_data=json.dumps(insights_data, default=str)
        )

        try:
            response = self._llm.generate(prompt)
            data = json.loads(response)
            return data.get("headline", "Work in progress")
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"At-a-glance generation failed: {e}")
            return "Work in progress"

    async def _generate_on_the_horizon(self, context: str) -> List[str]:
        """Generate emerging opportunities and upcoming work."""
        prompt = prompts.ON_THE_HORIZON_PROMPT.format(context=context)

        try:
            response = self._llm.generate(prompt)
            data = json.loads(response)
            items = []
            for key in [
                "emerging_patterns",
                "upcoming_features",
                "skill_development_areas",
            ]:
                items.extend(data.get(key, []))
            return items[:5]
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"On the horizon generation failed: {e}")
            return []
