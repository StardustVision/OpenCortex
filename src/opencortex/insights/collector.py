"""InsightsCollector - fetches session traces for insights analysis."""

import logging
import orjson
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from opencortex.alpha.types import Trace, Turn, TurnStatus, TraceOutcome

logger = logging.getLogger(__name__)


class InsightsCollector:
    """Fetches traces from TraceStore for a given date range."""

    def __init__(self, trace_store: Any, orchestrator: Any = None):
        """
        Initialize InsightsCollector.

        Args:
            trace_store: TraceStore instance for querying traces
            orchestrator: Unused, kept for backward compat
        """
        self._trace_store = trace_store

    async def fetch_traces(
        self,
        tenant_id: str,
        user_id: str,
        start_date: date,
        end_date: date,
    ) -> List[Trace]:
        """
        Fetch traces from TraceStore for the given date range.

        Queries Qdrant by tenant/user, filters by date range, and
        reconstructs Trace objects with turns loaded from CortexFS
        when available.

        Args:
            tenant_id: Tenant identifier
            user_id: User identifier
            start_date: Start date (inclusive)
            end_date: End date (inclusive)

        Returns:
            List of Trace objects
        """
        filter_expr = {
            "op": "and",
            "conditions": [
                {"field": "tenant_id", "op": "=", "value": tenant_id},
                {"field": "user_id", "op": "=", "value": user_id},
            ],
        }
        all_records = await self._trace_store._storage.filter(
            self._trace_store._collection, filter_expr, limit=1000,
        )

        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time())

        traces: List[Trace] = []
        for record in all_records:
            created_at_str = record.get("created_at", "")
            try:
                created_at = datetime.fromisoformat(created_at_str)
                # Normalize to naive for comparison if timezone-aware
                if created_at.tzinfo is not None:
                    created_at = created_at.replace(tzinfo=None)
                if not (start_dt <= created_at <= end_dt):
                    continue
            except (ValueError, TypeError):
                continue

            trace = self._record_to_trace(record)

            # Try to load turns from CortexFS if trace_store has a filesystem
            if not trace.turns and hasattr(self._trace_store, "_fs") and self._trace_store._fs:
                turns = await self._load_turns_from_fs(trace)
                if turns:
                    trace.turns = turns

            traces.append(trace)

        return traces

    def _record_to_trace(self, record: Dict[str, Any]) -> Trace:
        """Convert a Qdrant record dict to a Trace object."""
        # Parse outcome
        outcome = None
        outcome_str = record.get("outcome", "")
        if outcome_str:
            try:
                outcome = TraceOutcome(outcome_str)
            except ValueError:
                pass

        # Parse turns if stored inline
        turns: List[Turn] = []
        raw_turns = record.get("turns", [])
        if isinstance(raw_turns, list):
            for rt in raw_turns:
                if isinstance(rt, dict):
                    turns.append(self._dict_to_turn(rt))

        return Trace(
            trace_id=record.get("trace_id", record.get("id", "")),
            session_id=record.get("session_id", ""),
            tenant_id=record.get("tenant_id", ""),
            user_id=record.get("user_id", ""),
            source=record.get("source", ""),
            turns=turns,
            created_at=record.get("created_at", ""),
            source_version=record.get("source_version") or None,
            task_type=record.get("task_type") or None,
            outcome=outcome,
            error_code=record.get("error_code") or None,
            abstract=record.get("abstract") or None,
            overview=record.get("overview") or None,
        )

    def _dict_to_turn(self, d: Dict[str, Any]) -> Turn:
        """Convert a dict to a Turn object."""
        status_str = d.get("turn_status", "complete")
        try:
            status = TurnStatus(status_str)
        except ValueError:
            status = TurnStatus.COMPLETE

        return Turn(
            turn_id=d.get("turn_id", ""),
            prompt_text=d.get("prompt_text"),
            thought_text=d.get("thought_text"),
            tool_calls=d.get("tool_calls", []),
            final_text=d.get("final_text"),
            turn_status=status,
            latency_ms=d.get("latency_ms"),
            token_count=d.get("token_count"),
        )

    async def _load_turns_from_fs(self, trace: Trace) -> List[Turn]:
        """Load turns from CortexFS L2 content."""
        uri = (
            f"opencortex://{trace.tenant_id}/{trace.user_id}"
            f"/trace/{trace.trace_id}"
        )
        try:
            content = await self._trace_store._fs.read(uri)
            if not content:
                return []
            raw = orjson.loads(content)
            if isinstance(raw, list):
                return [self._dict_to_turn(d) for d in raw if isinstance(d, dict)]
        except Exception as e:
            logger.debug(f"Could not load turns for {trace.trace_id}: {e}")
        return []
