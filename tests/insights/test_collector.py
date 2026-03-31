"""Tests for InsightsCollector."""

import unittest
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Any
from unittest.mock import AsyncMock, MagicMock, patch

from opencortex.insights.types import SessionRecord, UserActivityWindow
from opencortex.insights.collector import InsightsCollector
from opencortex.alpha.types import Trace, TraceOutcome, Turn, TurnStatus


class MockStorage:
    """Mock Qdrant storage for testing."""

    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    async def filter(self, collection: str, filter_expr: dict, limit: int = 1000) -> List[Dict[str, Any]]:
        """Filter records by tenant_id and user_id from filter_expr."""
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

    def add_trace(self, trace: Dict[str, Any]):
        self._storage.records.append(trace)


class MockOrchestrator:
    """Mock MemoryOrchestrator for testing."""

    def __init__(self):
        self.memories: Dict[str, Dict[str, Any]] = {}

    async def list_memories(
        self,
        tenant_id: str,
        user_id: str,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        results = [
            m
            for m in self.memories.values()
            if m.get("tenant_id") == tenant_id and m.get("user_id") == user_id
        ]
        if category:
            results = [m for m in results if m.get("category") == category]
        return results[:limit]

    async def feedback(self, uri: str, reward: float) -> None:
        pass

    async def get_profile(self, uri: str) -> Optional[Dict[str, Any]]:
        return {
            "positive_feedback_count": 1,
            "reward_score": 0.5,
            "access_count": 3,
        }


class TestInsightsCollector(unittest.IsolatedAsyncioTestCase):
    """Test suite for InsightsCollector."""

    async def asyncSetUp(self):
        self.trace_store = MockTraceStore()
        self.orchestrator = MockOrchestrator()
        self.collector = InsightsCollector(
            trace_store=self.trace_store,
            orchestrator=self.orchestrator,
        )
        self.tenant_id = "test-tenant"
        self.user_id = "test-user"
        self.now = datetime.now()

    def _create_mock_trace(
        self,
        trace_id: str,
        session_id: str,
        user_message_count: int = 2,
        outcome: Optional[TraceOutcome] = None,
    ) -> Dict[str, Any]:
        turns = [
            {
                "turn_id": f"turn_{i}",
                "turn_status": TurnStatus.COMPLETE.value,
                "prompt_text": "user prompt" if i % 2 == 0 else None,
                "final_text": "response" if i % 2 == 1 else None,
            }
            for i in range(user_message_count * 2)
        ]
        return {
            "trace_id": trace_id,
            "session_id": session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "source": "claude_code",
            "turns": turns,
            "abstract": f"Abstract for {trace_id}",
            "overview": f"Overview for {trace_id}",
            "created_at": self.now.isoformat(),
            "outcome": outcome.value if outcome else TraceOutcome.SUCCESS.value,
            "message_count": user_message_count * 2,
            "user_message_count": user_message_count,
        }

    async def test_collect_user_sessions_empty(self):
        """Test collecting from empty trace store."""
        window = await self.collector.collect_user_sessions_async(
            self.tenant_id,
            self.user_id,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
            min_user_messages=1,
        )
        self.assertIsInstance(window, UserActivityWindow)
        self.assertEqual(window.sessions, 0)
        self.assertEqual(window.total_messages, 0)

    async def test_collect_user_sessions_single_session(self):
        """Test collecting from a single session."""
        trace1 = self._create_mock_trace(
            trace_id="trace_001",
            session_id="session_001",
            user_message_count=3,
            outcome=TraceOutcome.SUCCESS,
        )
        self.trace_store.add_trace(trace1)

        window = await self.collector.collect_user_sessions_async(
            self.tenant_id,
            self.user_id,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
            min_user_messages=1,
        )

        self.assertEqual(window.sessions, 1)
        self.assertGreater(window.total_messages, 0)

    def test_deduplication_by_session_id(self):
        """Test that deduplication keeps session with more user messages."""
        trace1 = self._create_mock_trace(
            trace_id="trace_001",
            session_id="session_001",
            user_message_count=2,
        )
        trace2 = self._create_mock_trace(
            trace_id="trace_002",
            session_id="session_001",
            user_message_count=5,
        )

        deduplicated = self.collector._deduplicate_sessions([trace1, trace2])
        self.assertEqual(len(deduplicated), 1)
        self.assertEqual(deduplicated[0]["trace_id"], "trace_002")

    async def test_filtering_by_min_user_messages(self):
        """Test filtering sessions below minimum user message threshold."""
        trace1 = self._create_mock_trace(
            trace_id="trace_001",
            session_id="session_001",
            user_message_count=1,
        )
        trace2 = self._create_mock_trace(
            trace_id="trace_002",
            session_id="session_002",
            user_message_count=5,
        )
        self.trace_store.add_trace(trace1)
        self.trace_store.add_trace(trace2)

        window = await self.collector.collect_user_sessions_async(
            self.tenant_id,
            self.user_id,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
            min_user_messages=3,
        )

        self.assertEqual(window.sessions, 1)

    def test_aggregate_window_computes_totals(self):
        """Test that aggregate_window computes correct totals."""
        sessions = [
            self._create_mock_trace(
                trace_id=f"trace_{i:03d}",
                session_id=f"session_{i:03d}",
                user_message_count=2,
            )
            for i in range(3)
        ]

        window = self.collector._aggregate_window(
            sessions,
            start_date=date.today() - timedelta(days=7),
            end_date=date.today(),
        )

        self.assertEqual(window.sessions, 3)
        self.assertGreater(window.total_messages, 0)
        self.assertGreater(window.total_tokens, 0)

    def test_enrich_with_memory_data(self):
        """Test enriching sessions with memory data."""
        trace = self._create_mock_trace(
            trace_id="trace_001",
            session_id="session_001",
            user_message_count=2,
        )
        self.orchestrator.memories["mem_001"] = {
            "uri": "opencortex://test-tenant/test-user/memory/notes/mem_001",
            "category": "notes",
            "abstract": "Test memory",
            "reward_score": 0.8,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
        }

        enriched = self.collector._enrich_with_memory_data(
            [trace], self.tenant_id, self.user_id
        )

        self.assertEqual(len(enriched), 1)
        self.assertGreater(enriched[0].get("memory_feedback_score", 0), 0)

    def test_session_record_conversion(self):
        """Test converting trace to SessionRecord."""
        trace = self._create_mock_trace(
            trace_id="trace_001",
            session_id="session_001",
            user_message_count=2,
            outcome=TraceOutcome.SUCCESS,
        )

        record = self.collector._trace_to_session_record(trace)

        self.assertIsInstance(record, SessionRecord)
        self.assertEqual(record.session_id, "session_001")
        self.assertEqual(record.tenant_id, self.tenant_id)
        self.assertEqual(record.user_id, self.user_id)
        self.assertEqual(record.user_message_count, 2)


if __name__ == "__main__":
    unittest.main()
