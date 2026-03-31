"""InsightsAgent - CC-equivalent 8-phase LLM analysis pipeline."""

import asyncio
import json
import logging
import statistics
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from opencortex.alpha.types import Trace
from opencortex.insights.cache import InsightsCache
from opencortex.insights.constants import (
    CHUNK_SIZE,
    FACET_CONCURRENCY,
    MAX_FACET_EXTRACTIONS,
    MAX_SESSIONS_TO_LOAD,
    MIN_DURATION_MINUTES,
    MIN_USER_MESSAGES,
    TRANSCRIPT_THRESHOLD,
)
from opencortex.insights.extractor import SessionMetaExtractor
from opencortex.insights.multi_clauding import detect_multi_clauding
from opencortex.insights.types import (
    AggregatedData,
    InsightsReport,
    SessionFacet,
    SessionMeta,
)
from opencortex.insights import prompts

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: deduplicate sessions
# ---------------------------------------------------------------------------

def deduplicate_sessions(
    entries: List[Tuple[Trace, SessionMeta]],
) -> List[Tuple[Trace, SessionMeta]]:
    """
    Deduplicate by session_id.
    Keep highest user_message_count; tie-break by duration.
    """
    best: Dict[str, Tuple[Trace, SessionMeta]] = {}
    for trace, meta in entries:
        sid = meta.session_id
        if sid not in best:
            best[sid] = (trace, meta)
        else:
            _, existing = best[sid]
            if (
                meta.user_message_count > existing.user_message_count
                or (
                    meta.user_message_count == existing.user_message_count
                    and meta.duration_minutes > existing.duration_minutes
                )
            ):
                best[sid] = (trace, meta)
    return list(best.values())


# ---------------------------------------------------------------------------
# Helper: filter substantive sessions
# ---------------------------------------------------------------------------

def filter_substantive(
    entries: List[Tuple[Trace, SessionMeta]],
) -> List[Tuple[Trace, SessionMeta]]:
    """Keep sessions with >= MIN_USER_MESSAGES and >= MIN_DURATION_MINUTES.

    Duration check is only applied when duration is known (> 0).
    The extractor may return 0.0 when timing data is unavailable.
    """
    return [
        (t, m) for t, m in entries
        if m.user_message_count >= MIN_USER_MESSAGES
        and (m.duration_minutes == 0.0 or m.duration_minutes >= MIN_DURATION_MINUTES)
    ]


# ---------------------------------------------------------------------------
# Helper: filter warmup-only sessions
# ---------------------------------------------------------------------------

def filter_warmup_only(
    entries: List[Tuple[Trace, SessionMeta]],
    facets: Dict[str, SessionFacet],
) -> List[Tuple[Trace, SessionMeta]]:
    """Remove sessions whose only goal category is warmup_minimal."""
    result = []
    for trace, meta in entries:
        facet = facets.get(meta.session_id)
        if facet is None:
            result.append((trace, meta))
            continue
        cats = facet.goal_categories
        if isinstance(cats, dict):
            keys = set(cats.keys())
        elif isinstance(cats, list):
            keys = set(cats)
        else:
            keys = set()
        if keys and keys == {"warmup_minimal"}:
            continue
        result.append((trace, meta))
    return result


# ---------------------------------------------------------------------------
# Helper: format transcript for facet extraction
# ---------------------------------------------------------------------------

def format_transcript_for_facets(trace: Trace, meta: SessionMeta) -> str:
    """
    Build a plain-text transcript from Trace turns, suitable for facet
    extraction.  Includes user prompts, assistant responses, and tool
    call names (but not full tool output).
    """
    lines: List[str] = []
    lines.append(f"[Session {meta.session_id} | {meta.duration_minutes:.0f}min | "
                 f"{meta.user_message_count} user msgs]")
    for turn in trace.turns:
        if turn.prompt_text:
            lines.append(f"User: {turn.prompt_text}")
        for tc in turn.tool_calls:
            name = tc.get("name", "")
            if name:
                lines.append(f"  [tool: {name}]")
        if turn.final_text:
            text = turn.final_text
            if len(text) > 500:
                text = text[:500] + "..."
            lines.append(f"Assistant: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helper: format transcript with chunked summarization for long sessions
# ---------------------------------------------------------------------------

async def format_transcript_with_summarization(
    trace: Trace,
    meta: SessionMeta,
    llm: Any,
) -> str:
    """
    For short transcripts, return plain text.  For long ones (> TRANSCRIPT_THRESHOLD),
    chunk into CHUNK_SIZE pieces, summarize each via LLM, and concatenate.
    """
    raw = format_transcript_for_facets(trace, meta)
    if len(raw) <= TRANSCRIPT_THRESHOLD:
        return raw

    # Chunk and summarize
    chunks: List[str] = []
    for i in range(0, len(raw), CHUNK_SIZE):
        chunks.append(raw[i : i + CHUNK_SIZE])

    summaries: List[str] = []
    for chunk in chunks:
        prompt = prompts.CHUNK_SUMMARY_PROMPT.format(chunk=chunk)
        try:
            resp = llm.generate(prompt)
            summaries.append(resp)
        except Exception as e:
            logger.warning(f"Chunk summarization failed: {e}")
            summaries.append(chunk[:300])

    return "\n---\n".join(summaries)


# ---------------------------------------------------------------------------
# Helper: aggregate data from metas and facets
# ---------------------------------------------------------------------------

def aggregate_data(
    metas: List[SessionMeta],
    facets: Dict[str, SessionFacet],
    start_date: date,
    end_date: date,
    total_scanned: int,
) -> AggregatedData:
    """Compute all AggregatedData fields from metas + facets."""
    # Merge tool_counts across all sessions
    tool_counts: Dict[str, int] = {}
    languages: Dict[str, int] = {}
    git_commits = 0
    git_pushes = 0
    total_input = 0
    total_output = 0
    total_messages = 0
    total_duration = 0.0
    total_interruptions = 0
    total_tool_errors = 0
    tool_error_cats: Dict[str, int] = {}
    all_response_times: List[float] = []
    all_message_hours: List[int] = []
    sessions_agent = 0
    sessions_mcp = 0
    sessions_web_search = 0
    sessions_web_fetch = 0
    total_lines_added = 0
    total_lines_removed = 0
    total_files_modified = 0
    projects: Dict[str, int] = {}
    start_times: List[str] = []

    for m in metas:
        # Tool counts
        for k, v in m.tool_counts.items():
            tool_counts[k] = tool_counts.get(k, 0) + v
        # Languages
        for k, v in m.languages.items():
            languages[k] = languages.get(k, 0) + v
        git_commits += m.git_commits
        git_pushes += m.git_pushes
        total_input += m.input_tokens
        total_output += m.output_tokens
        total_messages += m.user_message_count + m.assistant_message_count
        total_duration += m.duration_minutes
        total_interruptions += m.user_interruptions
        total_tool_errors += m.tool_errors
        for k, v in m.tool_error_categories.items():
            tool_error_cats[k] = tool_error_cats.get(k, 0) + v
        all_response_times.extend(m.user_response_times)
        all_message_hours.extend(m.message_hours)
        if m.uses_agent:
            sessions_agent += 1
        if m.uses_mcp:
            sessions_mcp += 1
        if m.uses_web_search:
            sessions_web_search += 1
        if m.uses_web_fetch:
            sessions_web_fetch += 1
        total_lines_added += m.lines_added
        total_lines_removed += m.lines_removed
        total_files_modified += m.files_modified
        if m.project_path:
            projects[m.project_path] = projects.get(m.project_path, 0) + 1
        if m.start_time:
            start_times.append(m.start_time)

    # Response time stats
    median_rt = statistics.median(all_response_times) if all_response_times else 0.0
    avg_rt = statistics.mean(all_response_times) if all_response_times else 0.0

    # Days active
    unique_dates: set = set()
    for st in start_times:
        try:
            dt = datetime.fromisoformat(st)
            unique_dates.add(dt.date())
        except (ValueError, TypeError):
            pass
    days_active = len(unique_dates)
    messages_per_day = total_messages / days_active if days_active else 0.0

    # Facet aggregations
    goal_categories: Dict[str, int] = {}
    outcomes: Dict[str, int] = {}
    satisfaction: Dict[str, int] = {}
    helpfulness: Dict[str, int] = {}
    session_types: Dict[str, int] = {}
    friction: Dict[str, int] = {}
    success: Dict[str, int] = {}
    session_summaries: List[Dict[str, str]] = []

    for sid, facet in facets.items():
        # goal_categories
        cats = facet.goal_categories
        if isinstance(cats, dict):
            for k, v in cats.items():
                goal_categories[k] = goal_categories.get(k, 0) + (v if isinstance(v, int) else 1)
        elif isinstance(cats, list):
            for k in cats:
                goal_categories[k] = goal_categories.get(k, 0) + 1
        # outcomes
        if facet.outcome:
            outcomes[facet.outcome] = outcomes.get(facet.outcome, 0) + 1
        # satisfaction
        if isinstance(facet.user_satisfaction_counts, dict):
            for k, v in facet.user_satisfaction_counts.items():
                satisfaction[k] = satisfaction.get(k, 0) + (v if isinstance(v, int) else 1)
        # helpfulness
        if facet.claude_helpfulness:
            h = facet.claude_helpfulness
            helpfulness[h] = helpfulness.get(h, 0) + 1
        # session_types
        if facet.session_type:
            session_types[facet.session_type] = session_types.get(facet.session_type, 0) + 1
        # friction
        if isinstance(facet.friction_counts, dict):
            for k, v in facet.friction_counts.items():
                friction[k] = friction.get(k, 0) + (v if isinstance(v, int) else 1)
        # success
        if facet.primary_success and facet.primary_success != "none":
            success[facet.primary_success] = success.get(facet.primary_success, 0) + 1
        # summaries
        if facet.brief_summary:
            session_summaries.append({
                "session_id": sid,
                "summary": facet.brief_summary,
            })

    # Multi-clauding
    multi_clauding = detect_multi_clauding(metas)

    return AggregatedData(
        total_sessions=len(metas),
        total_sessions_scanned=total_scanned,
        sessions_with_facets=len(facets),
        date_range={"start": str(start_date), "end": str(end_date)},
        total_messages=total_messages,
        total_duration_hours=total_duration / 60.0,
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        tool_counts=tool_counts,
        languages=languages,
        git_commits=git_commits,
        git_pushes=git_pushes,
        projects=projects,
        goal_categories=goal_categories,
        outcomes=outcomes,
        satisfaction=satisfaction,
        helpfulness=helpfulness,
        session_types=session_types,
        friction=friction,
        success=success,
        session_summaries=session_summaries,
        total_interruptions=total_interruptions,
        total_tool_errors=total_tool_errors,
        tool_error_categories=tool_error_cats,
        user_response_times=all_response_times,
        median_response_time=median_rt,
        avg_response_time=avg_rt,
        sessions_using_agent=sessions_agent,
        sessions_using_mcp=sessions_mcp,
        sessions_using_web_search=sessions_web_search,
        sessions_using_web_fetch=sessions_web_fetch,
        total_lines_added=total_lines_added,
        total_lines_removed=total_lines_removed,
        total_files_modified=total_files_modified,
        days_active=days_active,
        messages_per_day=messages_per_day,
        message_hours=all_message_hours,
        multi_clauding=multi_clauding,
    )


# ---------------------------------------------------------------------------
# Helper: build data_context string for LLM prompts
# ---------------------------------------------------------------------------

def build_data_context(agg: AggregatedData, facets: Dict[str, SessionFacet]) -> str:
    """Build a rich context string from aggregated data + top facets."""
    lines: List[str] = []

    lines.append(f"## Period: {agg.date_range['start']} to {agg.date_range['end']}")
    lines.append(f"Total sessions: {agg.total_sessions} (scanned: {agg.total_sessions_scanned})")
    lines.append(f"Total messages: {agg.total_messages}")
    lines.append(f"Duration: {agg.total_duration_hours:.1f}h")
    lines.append(f"Days active: {agg.days_active}")
    lines.append(f"Messages/day: {agg.messages_per_day:.1f}")
    lines.append("")

    # Top tools
    if agg.tool_counts:
        sorted_tools = sorted(agg.tool_counts.items(), key=lambda x: -x[1])[:15]
        lines.append("## Top Tools")
        for name, count in sorted_tools:
            lines.append(f"  {name}: {count}")
        lines.append("")

    # Languages
    if agg.languages:
        sorted_langs = sorted(agg.languages.items(), key=lambda x: -x[1])
        lines.append("## Languages")
        for name, count in sorted_langs:
            lines.append(f"  {name}: {count}")
        lines.append("")

    # Goals
    if agg.goal_categories:
        sorted_goals = sorted(agg.goal_categories.items(), key=lambda x: -x[1])
        lines.append("## Goal Categories")
        for name, count in sorted_goals:
            lines.append(f"  {name}: {count}")
        lines.append("")

    # Outcomes
    if agg.outcomes:
        lines.append("## Outcomes")
        for name, count in agg.outcomes.items():
            lines.append(f"  {name}: {count}")
        lines.append("")

    # Friction
    if agg.friction:
        sorted_friction = sorted(agg.friction.items(), key=lambda x: -x[1])
        lines.append("## Friction")
        for name, count in sorted_friction:
            lines.append(f"  {name}: {count}")
        lines.append("")

    # Git
    if agg.git_commits or agg.git_pushes:
        lines.append(f"## Git: {agg.git_commits} commits, {agg.git_pushes} pushes")
        lines.append("")

    # Code changes
    if agg.total_lines_added or agg.total_lines_removed:
        lines.append(f"## Code: +{agg.total_lines_added} / -{agg.total_lines_removed} lines, "
                      f"{agg.total_files_modified} files")
        lines.append("")

    # Response time
    if agg.median_response_time > 0:
        lines.append(f"## Response time: median {agg.median_response_time:.0f}s, "
                      f"avg {agg.avg_response_time:.0f}s")
        lines.append("")

    # Multi-clauding
    if agg.multi_clauding.get("overlap_events", 0) > 0:
        mc = agg.multi_clauding
        lines.append(f"## Multi-clauding: {mc['overlap_events']} overlaps, "
                      f"{mc['sessions_involved']} sessions involved")
        lines.append("")

    # Feature adoption
    features = []
    if agg.sessions_using_agent:
        features.append(f"Agent: {agg.sessions_using_agent}")
    if agg.sessions_using_mcp:
        features.append(f"MCP: {agg.sessions_using_mcp}")
    if agg.sessions_using_web_search:
        features.append(f"Web Search: {agg.sessions_using_web_search}")
    if agg.sessions_using_web_fetch:
        features.append(f"Web Fetch: {agg.sessions_using_web_fetch}")
    if features:
        lines.append("## Feature Adoption: " + ", ".join(features))
        lines.append("")

    # Top session summaries
    if agg.session_summaries:
        lines.append("## Session Summaries (top 20)")
        for s in agg.session_summaries[:20]:
            lines.append(f"  - [{s['session_id']}] {s['summary']}")
        lines.append("")

    # Top friction details from facets
    friction_details = []
    for facet in facets.values():
        if facet.friction_detail:
            friction_details.append(facet.friction_detail)
    if friction_details:
        lines.append("## Friction Details")
        for fd in friction_details[:10]:
            lines.append(f"  - {fd}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helper: generate parallel insights (7 sections)
# ---------------------------------------------------------------------------

async def generate_parallel_insights(
    data_context: str,
    llm: Any,
) -> Dict[str, Any]:
    """
    Generate 7 insight sections in parallel via asyncio.gather.

    Returns dict with keys: project_areas, interaction_style, what_works,
    friction_analysis, suggestions, on_the_horizon, fun_ending.
    """
    sections = {
        "project_areas": prompts.PROJECT_AREAS_PROMPT,
        "interaction_style": prompts.INTERACTION_STYLE_PROMPT,
        "what_works": prompts.WHAT_WORKS_PROMPT,
        "friction_analysis": prompts.FRICTION_ANALYSIS_PROMPT,
        "suggestions": prompts.SUGGESTIONS_PROMPT,
        "on_the_horizon": prompts.ON_THE_HORIZON_PROMPT,
        "fun_ending": prompts.FUN_ENDING_PROMPT,
    }

    async def _gen(name: str, template: str) -> Tuple[str, Any]:
        prompt = template.format(data_context=data_context)
        try:
            response = llm.generate(prompt)
            return (name, json.loads(response))
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Section {name} generation failed: {e}")
            return (name, {})

    tasks = [_gen(name, tmpl) for name, tmpl in sections.items()]
    results = await asyncio.gather(*tasks)
    return dict(results)


# ---------------------------------------------------------------------------
# InsightsAgent
# ---------------------------------------------------------------------------

class InsightsAgent:
    """CC-equivalent 8-phase insights analysis pipeline."""

    def __init__(
        self,
        llm: Any,
        collector: Any,
        cache: Optional[InsightsCache] = None,
    ):
        self._llm = llm
        self._collector = collector
        self._cache = cache
        self._extractor = SessionMetaExtractor()

    async def analyze_async(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> InsightsReport:
        """
        Run the 8-phase analysis pipeline.

        Phase 1: Load traces
        Phase 2: Extract SessionMeta per trace (with cache)
        Phase 3: Deduplicate + filter substantive
        Phase 4: Extract facets via LLM (with cache + concurrency limit)
        Phase 5: Filter warmup-only sessions
        Phase 6: Aggregate data
        Phase 7: Generate 7 sections in parallel
        Phase 8: Generate at_a_glance (depends on Phase 7)
        """
        llm_calls = 0
        cache_hits = 0

        # ---- Phase 1: Load traces ----
        traces = await self._collector.fetch_traces(
            tenant_id, user_id, start_date, end_date
        )
        traces = traces[:MAX_SESSIONS_TO_LOAD]
        total_scanned = len(traces)

        if not traces:
            return self._empty_report(tenant_id, user_id, start_date, end_date)

        # ---- Phase 2: Extract SessionMeta per trace ----
        entries: List[Tuple[Trace, SessionMeta]] = []
        for trace in traces:
            meta = None
            if self._cache:
                meta = await self._cache.get_meta(
                    tenant_id, user_id, trace.session_id
                )
                if meta is not None:
                    cache_hits += 1

            if meta is None:
                meta = self._extractor.extract(trace)
                if self._cache:
                    await self._cache.put_meta(
                        tenant_id, user_id, trace.session_id, meta
                    )

            entries.append((trace, meta))

        # ---- Phase 3: Deduplicate + filter substantive ----
        entries = deduplicate_sessions(entries)
        entries = filter_substantive(entries)

        if not entries:
            return self._empty_report(tenant_id, user_id, start_date, end_date)

        # ---- Phase 4: Extract facets via LLM (with cache) ----
        facets: Dict[str, SessionFacet] = {}
        uncached_entries: List[Tuple[Trace, SessionMeta]] = []

        for trace, meta in entries:
            if self._cache:
                cached_facet = await self._cache.get_facet(
                    tenant_id, user_id, meta.session_id
                )
                if cached_facet is not None:
                    facets[meta.session_id] = cached_facet
                    cache_hits += 1
                    continue

            uncached_entries.append((trace, meta))

        # Limit uncached extractions
        uncached_entries = uncached_entries[:MAX_FACET_EXTRACTIONS]

        sem = asyncio.Semaphore(FACET_CONCURRENCY)

        async def _extract_facet(
            trace: Trace, meta: SessionMeta,
        ) -> Optional[Tuple[str, SessionFacet]]:
            nonlocal llm_calls
            async with sem:
                transcript = await format_transcript_with_summarization(
                    trace, meta, self._llm
                )
                prompt = prompts.FACET_EXTRACTION_PROMPT.format(
                    transcript=transcript
                )
                try:
                    response = self._llm.generate(prompt)
                    llm_calls += 1
                    data = json.loads(response)
                    facet = SessionFacet(
                        session_id=meta.session_id,
                        underlying_goal=data.get("underlying_goal", "Unknown"),
                        goal_categories=data.get("goal_categories", {}),
                        outcome=data.get("outcome", "unclear_from_transcript"),
                        user_satisfaction_counts=data.get("user_satisfaction_counts", {}),
                        claude_helpfulness=data.get("claude_helpfulness", "moderately_helpful"),
                        session_type=data.get("session_type", "unknown"),
                        friction_counts=data.get("friction_counts", {}),
                        friction_detail=data.get("friction_detail", ""),
                        primary_success=data.get("primary_success", "none"),
                        brief_summary=data.get("brief_summary", ""),
                        user_instructions_to_claude=data.get("user_instructions_to_claude", []),
                    )
                    if self._cache:
                        await self._cache.put_facet(
                            tenant_id, user_id, meta.session_id, facet
                        )
                    return (meta.session_id, facet)
                except (json.JSONDecodeError, Exception) as e:
                    logger.warning(f"Facet extraction failed for {meta.session_id}: {e}")
                    return None

        facet_tasks = [_extract_facet(t, m) for t, m in uncached_entries]
        facet_results = await asyncio.gather(*facet_tasks)
        for result in facet_results:
            if result is not None:
                sid, facet = result
                facets[sid] = facet

        # ---- Phase 5: Filter warmup-only ----
        entries = filter_warmup_only(entries, facets)

        if not entries:
            return self._empty_report(tenant_id, user_id, start_date, end_date)

        # ---- Phase 6: Aggregate ----
        metas = [m for _, m in entries]
        agg = aggregate_data(metas, facets, start_date, end_date, total_scanned)

        # ---- Phase 7: Parallel section generation ----
        data_context = build_data_context(agg, facets)
        sections = await generate_parallel_insights(data_context, self._llm)
        llm_calls += 7  # 7 parallel sections

        # ---- Phase 8: At-a-glance (serial, depends on Phase 7) ----
        at_a_glance = self._generate_at_a_glance(data_context, sections)
        llm_calls += 1

        # ---- Assemble report ----
        return InsightsReport(
            tenant_id=tenant_id,
            user_id=user_id,
            report_period=f"{start_date} to {end_date}",
            generated_at=datetime.now(),
            total_sessions=len(entries),
            total_messages=agg.total_messages,
            total_duration_hours=agg.total_duration_hours,
            session_facets=list(facets.values()),
            project_areas=sections.get("project_areas", {}),
            what_works=sections.get("what_works", {}).get("impressive_workflows", []),
            friction_analysis=agg.friction,
            suggestions=sections.get("suggestions", {}),
            on_the_horizon=sections.get("on_the_horizon", {}),
            at_a_glance=at_a_glance,
            interaction_style=sections.get("interaction_style"),
            what_works_detail=sections.get("what_works"),
            friction_detail=sections.get("friction_analysis"),
            suggestions_detail=sections.get("suggestions"),
            on_the_horizon_detail=sections.get("on_the_horizon"),
            fun_ending=sections.get("fun_ending"),
            aggregated=asdict(agg),
            cache_hits=cache_hits,
            llm_calls=llm_calls,
        )

    def _generate_at_a_glance(
        self,
        data_context: str,
        sections: Dict[str, Any],
    ) -> Dict[str, str]:
        """
        Phase 8: Generate at-a-glance summary.
        Depends on all section outputs from Phase 7.
        """
        project_areas_text = json.dumps(sections.get("project_areas", {}), default=str)
        big_wins_text = json.dumps(sections.get("what_works", {}), default=str)
        friction_text = json.dumps(sections.get("friction_analysis", {}), default=str)
        suggestions = sections.get("suggestions", {})
        features_text = json.dumps(suggestions.get("features_to_try", []), default=str)
        patterns_text = json.dumps(suggestions.get("usage_patterns", []), default=str)
        horizon_text = json.dumps(sections.get("on_the_horizon", {}), default=str)

        prompt = prompts.AT_A_GLANCE_PROMPT.format(
            full_context=data_context,
            project_areas_text=project_areas_text,
            big_wins_text=big_wins_text,
            friction_text=friction_text,
            features_text=features_text,
            patterns_text=patterns_text,
            horizon_text=horizon_text,
        )

        try:
            response = self._llm.generate(prompt)
            data = json.loads(response)
            if isinstance(data, dict):
                return data
            return {"whats_working": str(data)}
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"At-a-glance generation failed: {e}")
            return {"whats_working": "Analysis in progress"}

    def _empty_report(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> InsightsReport:
        """Return an empty report when no data is available."""
        return InsightsReport(
            tenant_id=tenant_id,
            user_id=user_id,
            report_period=f"{start_date} to {end_date}",
            generated_at=datetime.now(),
            total_sessions=0,
            total_messages=0,
            total_duration_hours=0.0,
        )
