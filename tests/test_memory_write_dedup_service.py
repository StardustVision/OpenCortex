# SPDX-License-Identifier: Apache-2.0
"""Tests for write-time semantic deduplication service."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock

from opencortex.core.context import Context
from opencortex.http.request_context import (
    reset_request_project_id,
    set_request_project_id,
)
from opencortex.services.memory_signals import MemoryStoredSignal
from opencortex.services.memory_write_dedup_service import MemoryWriteDedupService


class _SignalBus:
    """Capture published signals for assertions."""

    def __init__(self) -> None:
        self.signals: List[Any] = []

    def publish_nowait(self, signal: Any) -> None:
        self.signals.append(signal)


class TestMemoryWriteDedupService(unittest.IsolatedAsyncioTestCase):
    """Verify the extracted write dedup boundary."""

    def _build_service(
        self,
        *,
        search_results: List[dict[str, Any]] | None = None,
        existing_record: dict[str, Any] | None = None,
    ) -> tuple[MemoryWriteDedupService, Any, Any]:
        storage = MagicMock()
        storage.search = AsyncMock(return_value=search_results or [])
        storage.filter = AsyncMock(return_value=[{"uri": "target-uri"}])
        fs = MagicMock()
        fs.read_file = AsyncMock(return_value="old content")
        orch = SimpleNamespace(
            _storage=storage,
            _fs=fs,
            _memory_signal_bus=_SignalBus(),
            _get_collection=MagicMock(return_value="context"),
            _get_record_by_uri=AsyncMock(
                return_value=existing_record
                if existing_record is not None
                else {
                    "id": "target-id",
                    "uri": "target-uri",
                    "project_id": "project-9",
                    "context_type": "memory",
                    "category": "preferences",
                }
            ),
        )
        memory_service = SimpleNamespace(
            update=AsyncMock(),
            feedback=AsyncMock(),
        )
        write_service = SimpleNamespace(_orch=orch, _service=memory_service)
        return MemoryWriteDedupService(write_service), orch, memory_service

    async def test_check_duplicate_builds_scope_and_project_filter(self) -> None:
        """Duplicate search applies tenant, scope, kind, signature, and project."""
        service, orch, _memory_service = self._build_service(
            search_results=[{"uri": "target-uri", "_score": 0.91}]
        )
        token = set_request_project_id("project-9")
        try:
            duplicate = await service.check_duplicate(
                vector=[0.1, 0.2],
                memory_kind="preference",
                merge_signature="sig-1",
                threshold=0.82,
                tid="tenant-1",
                uid="user-1",
            )
        finally:
            reset_request_project_id(token)

        self.assertEqual(duplicate, ("target-uri", 0.91))
        orch._storage.search.assert_awaited_once()
        _, kwargs = orch._storage.search.await_args
        self.assertEqual(kwargs["query_vector"], [0.1, 0.2])
        self.assertEqual(kwargs["limit"], 1)
        self.assertEqual(kwargs["output_fields"], ["uri", "abstract"])
        self.assertEqual(
            kwargs["filter"],
            {
                "op": "and",
                "conds": [
                    {
                        "op": "must",
                        "field": "source_tenant_id",
                        "conds": ["tenant-1"],
                    },
                    {"op": "must", "field": "is_leaf", "conds": [True]},
                    {
                        "op": "must",
                        "field": "memory_kind",
                        "conds": ["preference"],
                    },
                    {
                        "op": "must",
                        "field": "merge_signature",
                        "conds": ["sig-1"],
                    },
                    {
                        "op": "or",
                        "conds": [
                            {"op": "must", "field": "scope", "conds": ["shared"]},
                            {
                                "op": "and",
                                "conds": [
                                    {
                                        "op": "must",
                                        "field": "scope",
                                        "conds": ["private"],
                                    },
                                    {
                                        "op": "must",
                                        "field": "source_user_id",
                                        "conds": ["user-1"],
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "op": "must",
                        "field": "project_id",
                        "conds": ["project-9"],
                    },
                ],
            },
        )

    async def test_check_duplicate_rejects_score_below_threshold(self) -> None:
        """Scores below the configured threshold are not duplicates."""
        service, _orch, _memory_service = self._build_service(
            search_results=[{"uri": "target-uri", "_score": 0.5}]
        )

        duplicate = await service.check_duplicate(
            vector=[0.1],
            memory_kind="preference",
            merge_signature="sig-1",
            threshold=0.82,
            tid="tenant-1",
            uid="user-1",
        )

        self.assertIsNone(duplicate)

    async def test_try_merge_duplicate_merges_and_publishes_signal(self) -> None:
        """A duplicate hit merges into the target and emits a merge signal."""
        existing_record = {
            "id": "target-id",
            "uri": "target-uri",
            "project_id": "project-9",
            "context_type": "memory",
            "category": "preferences",
        }
        service, orch, memory_service = self._build_service(
            search_results=[{"uri": "target-uri", "_score": 0.93}],
            existing_record=existing_record,
        )
        ctx = Context(
            uri="new-uri",
            abstract="new abstract",
            context_type="memory",
            category="preferences",
            id="new-id",
        )

        result = await service.try_merge_duplicate(
            ctx=ctx,
            vector=[0.1],
            memory_kind="preference",
            merge_signature="sig-1",
            threshold=0.82,
            tenant_id="tenant-1",
            user_id="user-1",
            abstract="new abstract",
            content="new content",
            add_started=asyncio.get_running_loop().time(),
        )

        self.assertTrue(result.merged)
        self.assertIs(result.ctx, ctx)
        self.assertEqual(result.target_uri, "target-uri")
        self.assertEqual(result.score, 0.93)
        self.assertEqual(ctx.uri, "target-uri")
        self.assertEqual(ctx.meta["dedup_action"], "merged")
        self.assertEqual(ctx.meta["dedup_score"], 0.93)
        memory_service.update.assert_awaited_once_with(
            "target-uri",
            abstract="new abstract",
            content="old content\n---\nnew content",
        )
        memory_service.feedback.assert_awaited_once_with("target-uri", 0.5)

        self.assertEqual(len(orch._memory_signal_bus.signals), 1)
        signal = orch._memory_signal_bus.signals[0]
        self.assertIsInstance(signal, MemoryStoredSignal)
        self.assertEqual(signal.uri, "target-uri")
        self.assertEqual(signal.record_id, "target-id")
        self.assertEqual(signal.tenant_id, "tenant-1")
        self.assertEqual(signal.user_id, "user-1")
        self.assertEqual(signal.project_id, "project-9")
        self.assertEqual(signal.context_type, "memory")
        self.assertEqual(signal.category, "preferences")
        self.assertEqual(signal.dedup_action, "merged")
        self.assertEqual(signal.record, existing_record)

    async def test_merge_into_without_new_content_preserves_existing_content(
        self,
    ) -> None:
        """Empty incoming content keeps the existing filesystem content."""
        service, _orch, memory_service = self._build_service()

        await service.merge_into(
            "target-uri",
            new_abstract="new abstract",
            new_content="",
        )

        memory_service.update.assert_awaited_once_with(
            "target-uri",
            abstract="new abstract",
            content="old content",
        )
        memory_service.feedback.assert_awaited_once_with("target-uri", 0.5)
