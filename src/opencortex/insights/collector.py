"""InsightsCollector - collects and aggregates user session data for insights analysis."""

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from opencortex.insights.types import SessionRecord, UserActivityWindow

logger = logging.getLogger(__name__)


class InsightsCollector:
    """Collects session data from TraceStore and enriches with memory data."""

    def __init__(self, trace_store: Any, orchestrator: Any):
        """
        Initialize InsightsCollector.

        Args:
            trace_store: TraceStore instance for querying traces
            orchestrator: MemoryOrchestrator instance for memory enrichment
        """
        self._trace_store = trace_store
        self._orchestrator = orchestrator

    def collect_user_sessions(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
        min_user_messages: int = 1,
    ) -> UserActivityWindow:
        """
        Main entry point - collect user sessions within a date window.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            min_user_messages: Minimum user messages to include session

        Returns:
            UserActivityWindow with aggregated session data
        """
        sessions = self._fetch_sessions_from_traces(
            tenant_id, user_id, start_date, end_date
        )

        # Deduplicate by session_id, keeping the one with more user messages
        sessions = self._deduplicate_sessions(sessions)

        # Filter by minimum user messages
        sessions = [
            s for s in sessions if s.get("user_message_count", 0) >= min_user_messages
        ]

        # Enrich with memory data
        sessions = self._enrich_with_memory_data(sessions, tenant_id, user_id)

        # Aggregate into window
        window = self._aggregate_window(sessions, start_date, end_date)

        return window

    async def collect_user_sessions_async(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
        min_user_messages: int = 1,
    ) -> UserActivityWindow:
        """
        Async version of collect_user_sessions.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            min_user_messages: Minimum user messages to include session

        Returns:
            UserActivityWindow with aggregated session data
        """
        sessions = await self._fetch_sessions_from_traces_async(
            tenant_id, user_id, start_date, end_date
        )

        # Deduplicate by session_id
        sessions = self._deduplicate_sessions(sessions)

        # Filter by minimum user messages
        sessions = [
            s for s in sessions if s.get("user_message_count", 0) >= min_user_messages
        ]

        # Enrich with memory data
        sessions = self._enrich_with_memory_data(sessions, tenant_id, user_id)

        # Aggregate into window
        window = self._aggregate_window(sessions, start_date, end_date)

        return window

    def _fetch_sessions_from_traces(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> List[Dict[str, Any]]:
        """
        Query TraceStore for sessions in date range.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
            start_date: Start date (inclusive)
            end_date: End date (inclusive)

        Returns:
            List of trace records
        """
        # Convert dates to datetime range
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time())

        # Query with a broad search to get all traces
        # In production, TraceStore would support date range filtering
        sessions = self._trace_store.traces.copy()

        # Filter by date range and tenant/user
        filtered = []
        for trace in sessions.values():
            if trace.get("tenant_id") != tenant_id:
                continue
            if trace.get("user_id") != user_id:
                continue

            created_at_str = trace.get("created_at", "")
            try:
                created_at = datetime.fromisoformat(created_at_str)
                if start_dt <= created_at <= end_dt:
                    filtered.append(trace)
            except (ValueError, TypeError):
                # Skip traces with invalid timestamps
                continue

        return filtered

    async def _fetch_sessions_from_traces_async(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> List[Dict[str, Any]]:
        """
        Async version of _fetch_sessions_from_traces.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
            start_date: Start date (inclusive)
            end_date: End date (inclusive)

        Returns:
            List of trace records
        """
        # For now, delegate to sync version in async context
        # In production, this would use async trace store methods
        return self._fetch_sessions_from_traces(
            tenant_id, user_id, start_date, end_date
        )

    def _deduplicate_sessions(
        self, sessions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Remove duplicates by session_id, keeping the one with more user messages.

        Args:
            sessions: List of trace records

        Returns:
            Deduplicated list of trace records
        """
        session_map: Dict[str, Dict[str, Any]] = {}

        for session in sessions:
            session_id = session.get("session_id")
            if not session_id:
                continue

            if session_id not in session_map:
                session_map[session_id] = session
            else:
                # Keep the one with more user messages
                existing_count = session_map[session_id].get("user_message_count", 0)
                new_count = session.get("user_message_count", 0)
                if new_count > existing_count:
                    session_map[session_id] = session

        return list(session_map.values())

    def _enrich_with_memory_data(
        self,
        sessions: List[Dict[str, Any]],
        tenant_id: str,
        user_id: str,
    ) -> List[Dict[str, Any]]:
        """
        Add memory stats and feedback scores to sessions.

        Args:
            sessions: List of trace records
            tenant_id: Tenant identifier
            user_id: User identifier

        Returns:
            Enriched list of trace records
        """
        enriched = []

        for session in sessions:
            session_copy = session.copy()

            # Get memory stats for the user
            memories = self._orchestrator.memories.copy()
            user_memories = [
                m
                for m in memories.values()
                if m.get("tenant_id") == tenant_id and m.get("user_id") == user_id
            ]

            # Calculate average feedback score
            total_score = sum(m.get("reward_score", 0) for m in user_memories)
            avg_score = total_score / len(user_memories) if user_memories else 0

            session_copy["memory_feedback_score"] = avg_score
            session_copy["memories_created"] = len(user_memories)

            enriched.append(session_copy)

        return enriched

    def _aggregate_window(
        self,
        sessions: List[Dict[str, Any]],
        start_date: date,
        end_date: date,
    ) -> UserActivityWindow:
        """
        Build UserActivityWindow from list of sessions.

        Args:
            sessions: List of session records
            start_date: Start date of the window
            end_date: End date of the window

        Returns:
            UserActivityWindow with aggregated data
        """
        window = UserActivityWindow(
            start_date=start_date,
            end_date=end_date,
            sessions=len(sessions),
            total_messages=sum(s.get("message_count", 0) for s in sessions),
            total_tokens=sum(s.get("token_count", 0) for s in sessions)
            or len(sessions) * 100,
            unique_projects=1,  # Placeholder, would count unique project_ids
            tool_usage={},
            memory_feedback_score=sum(
                s.get("memory_feedback_score", 0) for s in sessions
            )
            / len(sessions)
            if sessions
            else 0,
        )

        return window

    def _trace_to_session_record(self, trace: Dict[str, Any]) -> SessionRecord:
        """
        Convert a trace record to SessionRecord.

        Args:
            trace: Trace record from TraceStore

        Returns:
            SessionRecord instance
        """
        created_at = trace.get("created_at", datetime.utcnow().isoformat())
        try:
            started_at = datetime.fromisoformat(created_at)
        except (ValueError, TypeError):
            started_at = datetime.utcnow()

        # Assume 1 minute duration for simplicity
        ended_at = started_at + timedelta(minutes=1)

        return SessionRecord(
            session_id=trace.get("session_id", ""),
            tenant_id=trace.get("tenant_id", ""),
            user_id=trace.get("user_id", ""),
            project_id="",  # Would be extracted from context
            started_at=started_at,
            ended_at=ended_at,
            message_count=trace.get("message_count", 0),
            user_message_count=trace.get("user_message_count", 0),
            tool_calls=len(trace.get("turns", [])),
            memories_created=trace.get("memories_created", 0),
            memories_referenced=trace.get("memories_referenced", 0),
            feedback_given=0,  # Would be tracked separately
            session_type="unknown",  # Would be classified from content
            outcome=trace.get("outcome"),
        )
