# SPDX-License-Identifier: Apache-2.0
"""Tests for memory query/list service helpers."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Coroutine
from unittest.mock import AsyncMock, MagicMock

from opencortex.services.memory_query_service import MemoryQueryService


class TestMemoryQueryServiceAdminList(unittest.TestCase):
    """Verify admin listing keeps its filter contract."""

    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run(coro)

    def test_list_memories_admin_builds_filter_with_optional_clauses(self) -> None:
        """Admin list uses filter expr without changing filter shape."""
        orch = MagicMock()
        orch._ensure_init.return_value = None
        orch._get_collection.return_value = "context"
        orch._storage.filter = AsyncMock(
            return_value=[
                {
                    "uri": "u1",
                    "abstract": "hello",
                    "category": "events",
                    "context_type": "memory",
                    "scope": "private",
                    "project_id": "public",
                    "source_tenant_id": "tenant-a",
                    "source_user_id": "user-b",
                    "updated_at": "2026-05-04T00:00:00Z",
                    "created_at": "2026-05-03T00:00:00Z",
                },
                {"uri": "dir", "abstract": ""},
            ],
        )
        memory_service = MagicMock()
        memory_service._orch = orch
        service = MemoryQueryService(memory_service)

        result = self._run(
            service.list_memories_admin(
                tenant_id="tenant-a",
                user_id="user-b",
                category="events",
                context_type="memory",
                limit=10,
                offset=5,
            )
        )

        orch._storage.filter.assert_awaited_once_with(
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
                    {"op": "must", "field": "category", "conds": ["events"]},
                    {"op": "must", "field": "context_type", "conds": ["memory"]},
                ],
            },
            limit=10,
            offset=5,
            order_by="updated_at",
            order_desc=True,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["uri"], "u1")


if __name__ == "__main__":
    unittest.main()
