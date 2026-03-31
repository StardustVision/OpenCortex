"""Tests for InsightsCollector - simplified trace fetching."""

import unittest
from datetime import datetime, timedelta, date, timezone
from typing import Any, Dict, List, Optional

from opencortex.alpha.types import Trace, Turn, TurnStatus, TraceOutcome
from opencortex.insights.collector import InsightsCollector


class MockStorage:
    """Mock Qdrant storage for testing."""

    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    async def filter(
        self, collection: str, filter_expr: dict, limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        conditions = filter_expr.get("conditions", [])
        tid = uid = None
        for c in conditions:
            if c.get("field") == "tenant_id":
                tid = c.get("value")
            if c.get("field") == "user_id":
                uid = c.get("value")

        results = []
        for r in self.records:
            if tid and r.get("tenant_id") != tid:
                continue
            if uid and r.get("user_id") != uid:
                continue
            results.append(r)
        return results[:limit]


class MockTraceStore:
    """Mock TraceStore for testing."""

    def __init__(self):
        self._storage = MockStorage()
        self._collection = "traces"
        self._fs = None

    def add_record(self, record: Dict[str, Any]):
        self._storage.records.append(record)


class TestInsightsCollector(unittest.IsolatedAsyncioTestCase):
    """Test suite for simplified InsightsCollector."""

    async def asyncSetUp(self):
        self.trace_store = MockTraceStore()
        self.collector = InsightsCollector(trace_store=self.trace_store)
        self.tenant_id = "test-tenant"
        self.user_id = "test-user"
        self.now = datetime.now(timezone.utc)

    def _add_trace_record(
        self,
        trace_id: str = "trace-1",
        session_id: str = "session-1",
        user_message_count: int = 3,
        created_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Add a trace record to the mock store and return it."""
        dt = created_at or self.now
        turns = [
            {
                "turn_id": f"turn_{i}",
                "turn_status": "complete",
                "prompt_text": f"user msg {i}" if i % 2 == 0 else None,
                "final_text": f"response {i}" if i % 2 == 1 else None,
                "tool_calls": [],
            }
            for i in range(user_message_count * 2)
        ]
        record = {
            "id": trace_id,
            "trace_id": trace_id,
            "session_id": session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "source": "claude_code",
            "turns": turns,
            "abstract": f"Abstract for {trace_id}",
            "overview": f"Overview for {trace_id}",
            "created_at": dt.isoformat(),
            "outcome": "success",
        }
        self.trace_store.add_record(record)
        return record

    async def test_fetch_traces_empty(self):
        """Empty trace store returns empty list."""
        traces = await self.collector.fetch_traces(
            self.tenant_id, self.user_id,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
        )
        self.assertEqual(traces, [])

    async def test_fetch_traces_returns_trace_objects(self):
        """Fetched results are Trace instances with turns."""
        self._add_trace_record("t1", "s1", user_message_count=3)

        traces = await self.collector.fetch_traces(
            self.tenant_id, self.user_id,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
        )

        self.assertEqual(len(traces), 1)
        self.assertIsInstance(traces[0], Trace)
        self.assertEqual(traces[0].session_id, "s1")
        self.assertEqual(traces[0].trace_id, "t1")
        self.assertGreater(len(traces[0].turns), 0)

    async def test_fetch_traces_filters_by_date(self):
        """Only traces within date range are returned."""
        # Within range
        self._add_trace_record("t1", "s1", created_at=self.now)
        # Outside range (30 days ago)
        self._add_trace_record(
            "t2", "s2",
            created_at=self.now - timedelta(days=30),
        )

        traces = await self.collector.fetch_traces(
            self.tenant_id, self.user_id,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
        )

        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].session_id, "s1")

    async def test_fetch_traces_filters_by_tenant(self):
        """Only traces for the correct tenant are returned."""
        self._add_trace_record("t1", "s1")
        # Different tenant
        other_record = {
            "trace_id": "t2", "session_id": "s2",
            "tenant_id": "other-tenant", "user_id": self.user_id,
            "source": "claude_code", "turns": [],
            "created_at": self.now.isoformat(),
        }
        self.trace_store.add_record(other_record)

        traces = await self.collector.fetch_traces(
            self.tenant_id, self.user_id,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
        )

        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0].tenant_id, self.tenant_id)

    async def test_turns_reconstructed_from_inline(self):
        """Turns stored inline in the record are properly reconstructed."""
        self._add_trace_record("t1", "s1", user_message_count=2)

        traces = await self.collector.fetch_traces(
            self.tenant_id, self.user_id,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
        )

        trace = traces[0]
        self.assertEqual(len(trace.turns), 4)  # 2 user + 2 assistant turns
        self.assertIsInstance(trace.turns[0], Turn)

    async def test_outcome_parsed(self):
        """TraceOutcome is properly parsed from record."""
        self._add_trace_record("t1", "s1")

        traces = await self.collector.fetch_traces(
            self.tenant_id, self.user_id,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
        )

        self.assertEqual(traces[0].outcome, TraceOutcome.SUCCESS)


if __name__ == "__main__":
    unittest.main()
