# SPDX-License-Identifier: Apache-2.0
"""Tests for ``MemorySharingService``."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from opencortex.http.request_context import (
    reset_request_identity,
    set_request_identity,
)
from opencortex.services.memory_sharing_service import MemorySharingService


class TestPromoteToShared(unittest.TestCase):
    """Verify memory sharing/admin mutation behavior."""

    def _run(self, coro):
        return asyncio.run(coro)

    def _make_orchestrator(self, records):
        mock_orch = MagicMock()
        mock_orch._ensure_init.return_value = None
        mock_orch._get_collection.return_value = "context"
        mock_orch._storage.filter = AsyncMock(return_value=records)
        mock_orch._storage.upsert = AsyncMock(return_value="record-1")
        mock_orch._storage.delete = AsyncMock(return_value=1)
        return mock_orch

    def test_promote_updates_record_without_deleting_same_id(self) -> None:
        """Promoting in-place must not delete the newly upserted point."""
        tokens = set_request_identity("tenant-1", "user-1")
        try:
            record = {
                "id": "record-1",
                "uri": "opencortex://tenant-1/user/user-1/resources/docs/note",
                "scope": "private",
                "project_id": "",
                "parent_uri": "",
            }
            mock_orch = self._make_orchestrator([record])
            service = MemorySharingService(mock_orch)

            result = self._run(
                service.promote_to_shared(
                    ["opencortex://tenant-1/user/user-1/resources/docs/note"],
                    "project-a",
                )
            )
        finally:
            reset_request_identity(tokens)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["promoted"], 1)
        mock_orch._storage.upsert.assert_awaited_once()
        mock_orch._storage.delete.assert_not_called()
        promoted = mock_orch._storage.upsert.await_args.args[1]
        self.assertEqual(promoted["scope"], "shared")
        self.assertEqual(promoted["project_id"], "project-a")
        self.assertEqual(
            promoted["uri"],
            "opencortex://tenant-1/resources/project-a/documents/note",
        )

    def test_promote_reports_not_found(self) -> None:
        """Missing URI returns a partial result with per-item error."""
        mock_orch = self._make_orchestrator([])
        service = MemorySharingService(mock_orch)

        result = self._run(service.promote_to_shared(["missing"], "project-a"))

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["errors"], [{"uri": "missing", "error": "not found"}])
        mock_orch._storage.upsert.assert_not_called()
