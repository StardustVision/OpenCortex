# SPDX-License-Identifier: Apache-2.0
"""Tests for ``RetrievalCandidateService``."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Awaitable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from opencortex.retrieve.types import ContextType, DetailLevel
from opencortex.services.retrieval_candidate_service import RetrievalCandidateService


class TestRetrievalCandidateService(unittest.TestCase):
    """Verify candidate projection helper behavior."""

    def _run(self, coro: Awaitable[Any]) -> Any:
        return asyncio.run(coro)

    def _make_service(self) -> RetrievalCandidateService:
        retrieval_service = MagicMock()
        retrieval_service._fs = None
        return RetrievalCandidateService(retrieval_service)

    def test_record_passes_acl_preserves_private_and_project_rules(self) -> None:
        """ACL allows own private/project records and rejects mismatches."""
        service = self._make_service()
        self.assertTrue(
            service._record_passes_acl(
                {
                    "scope": "private",
                    "source_tenant_id": "tenant-a",
                    "source_user_id": "user-a",
                    "project_id": "project-a",
                },
                "tenant-a",
                "user-a",
                "project-a",
            )
        )
        self.assertFalse(
            service._record_passes_acl(
                {
                    "scope": "private",
                    "source_tenant_id": "tenant-a",
                    "source_user_id": "user-b",
                    "project_id": "project-a",
                },
                "tenant-a",
                "user-a",
                "project-a",
            )
        )
        self.assertFalse(
            service._record_passes_acl(
                {
                    "scope": "shared",
                    "source_tenant_id": "tenant-b",
                    "source_user_id": "user-a",
                    "project_id": "project-a",
                },
                "tenant-a",
                "user-a",
                "project-a",
            )
        )
        self.assertFalse(
            service._record_passes_acl(
                {
                    "scope": "shared",
                    "source_tenant_id": "tenant-a",
                    "source_user_id": "user-a",
                    "project_id": "project-b",
                },
                "tenant-a",
                "user-a",
                "project-a",
            )
        )

    def test_matched_record_anchors_returns_intersections_capped_to_eight(self) -> None:
        """Anchor projection returns deterministic matching query anchors."""
        service = self._make_service()
        record = {
            "abstract_json": {
                "anchors": [
                    {"anchor_type": "entity", "text": f"v{i}"} for i in range(10)
                ]
            }
        }
        query_groups = {"entity": {f"v{i}" for i in range(10)}}

        result = service._matched_record_anchors(
            record=record,
            query_anchor_groups=query_groups,
        )

        self.assertEqual(result, [f"v{i}" for i in range(8)])

    def test_records_to_matched_contexts_uses_l2_filesystem_fallback(self) -> None:
        """L2 projection reads content from CortexFS when payload lacks content."""
        retrieval_service = MagicMock()
        retrieval_service._fs.read_file = AsyncMock(return_value="file content")
        service = RetrievalCandidateService(retrieval_service)

        result = self._run(
            service._records_to_matched_contexts(
                candidates=[
                    {
                        "uri": "opencortex://tenant/user/memories/events/item",
                        "context_type": "memory",
                        "is_leaf": True,
                        "abstract": "abstract",
                        "overview": "overview",
                        "_final_score": 0.75,
                        "_match_reason": "semantic",
                        "meta": {"source_uri": "source", "msg_range": [1, 2]},
                    }
                ],
                context_type=ContextType.ANY,
                detail_level=DetailLevel.L2,
            )
        )

        self.assertEqual(len(result), 1)
        match = result[0]
        self.assertEqual(match.context_type, ContextType.MEMORY)
        self.assertEqual(match.content, "file content")
        self.assertEqual(match.overview, "overview")
        self.assertEqual(match.score, 0.75)
        self.assertEqual(match.source_uri, "source")
        self.assertEqual(match.msg_range, [1, 2])
        retrieval_service._fs.read_file.assert_awaited_once_with(
            "opencortex://tenant/user/memories/events/item/content.md"
        )
