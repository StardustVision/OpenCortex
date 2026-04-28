# SPDX-License-Identifier: Apache-2.0
"""Tests for ``MemoryAdminStatsService``."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from opencortex.services.memory_admin_stats_service import MemoryAdminStatsService


class TestMemoryAdminStatsService(unittest.TestCase):
    """Verify admin memory statistics behavior."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_orchestrator(self, memories):
        mock_orch = MagicMock()
        mock_orch._ensure_init.return_value = None
        mock_orch._get_collection.return_value = "context"
        mock_orch._storage.filter = AsyncMock(return_value=memories)
        return mock_orch

    def test_get_user_memory_stats_builds_filter_and_totals(self) -> None:
        """Stats query preserves filter shape and aggregate output."""
        memories = [
            {
                "session_id": "s1",
                "positive_feedback_count": 2,
                "negative_feedback_count": 1,
            },
            {
                "session_id": "s1",
                "positive_feedback_count": 0,
                "negative_feedback_count": 3,
            },
            {
                "session_id": "s2",
                "positive_feedback_count": 4,
                "negative_feedback_count": 0,
            },
        ]
        mock_orch = self._make_orchestrator(memories)
        service = MemoryAdminStatsService(mock_orch)

        result = self._run(service.get_user_memory_stats("tenant-a", "user-b"))

        mock_orch._ensure_init.assert_called_once()
        mock_orch._storage.filter.assert_awaited_once_with(
            "context",
            {
                "op": "and",
                "conds": [
                    {
                        "op": "must_not",
                        "field": "context_type",
                        "conds": ["staging"],
                    },
                    {
                        "op": "must",
                        "field": "source_tenant_id",
                        "conds": ["tenant-a"],
                    },
                    {
                        "op": "must",
                        "field": "source_user_id",
                        "conds": ["user-b"],
                    },
                ],
            },
            limit=10000,
        )
        self.assertEqual(result["created_in_session"], {"s1": 2, "s2": 1})
        self.assertEqual(result["total_memories"], 3)
        self.assertEqual(result["total_positive_feedback"], 6)
        self.assertEqual(result["total_negative_feedback"], 4)

    def test_get_user_memory_stats_empty_result(self) -> None:
        """Empty stats query returns stable zero-valued shape."""
        mock_orch = self._make_orchestrator([])
        service = MemoryAdminStatsService(mock_orch)

        result = self._run(service.get_user_memory_stats("tenant-a", "user-b"))

        self.assertEqual(
            result,
            {
                "created_in_session": {},
                "total_memories": 0,
                "total_positive_feedback": 0,
                "total_negative_feedback": 0,
            },
        )


class TestOrchestratorMemoryAdminStatsProperty(unittest.TestCase):
    """Lock the lazy-property contract for memory admin stats."""

    def test_lazy_property_works_on_new_bypassed_orchestrator(self) -> None:
        """``__new__`` bypass + first property access succeeds."""
        from opencortex.orchestrator import MemoryOrchestrator

        orch = MemoryOrchestrator.__new__(MemoryOrchestrator)
        service = orch._memory_admin_stats_service
        self.assertIsNotNone(service)
        self.assertIs(orch._memory_admin_stats_service, service)
