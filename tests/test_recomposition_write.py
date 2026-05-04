# SPDX-License-Identifier: Apache-2.0
"""Tests for recomposition write helpers."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any, Coroutine
from unittest.mock import AsyncMock, MagicMock

from opencortex.context.recomposition_tasks import ContextRecompositionTaskService
from opencortex.context.recomposition_write import RecompositionWriteService


class TestRecompositionWriteService(unittest.TestCase):
    """Lock recomposition write/persistence behavior behind the write service."""

    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run(coro)

    def _make_manager(self) -> MagicMock:
        manager = MagicMock()
        manager._derive_semaphore = asyncio.Semaphore(3)
        manager._recomposition_tasks = ContextRecompositionTaskService(manager)
        manager._session_summary_uri.side_effect = (
            lambda tenant_id, user_id, session_id: (
                f"opencortex://{tenant_id}/{user_id}/session/conversations/"
                f"{session_id}/summary"
            )
        )
        manager._orchestrator._get_collection.return_value = "context"
        manager._orchestrator._storage.filter = AsyncMock(
            return_value=[{"id": "record-id"}]
        )
        manager._orchestrator._storage.update = AsyncMock()
        manager._orchestrator.add = AsyncMock(
            side_effect=lambda **kwargs: SimpleNamespace(uri=kwargs["uri"])
        )
        manager._orchestrator._complete_deferred_derive = AsyncMock()
        manager._orchestrator._fs = None
        return manager

    def test_uri_helpers_keep_stable_patterns(self) -> None:
        """URI helper output remains stable for compatibility wrappers."""
        merged_uri = RecompositionWriteService.merged_leaf_uri(
            "team",
            "user",
            "session-abc",
            [2, 4],
        )
        directory_uri = RecompositionWriteService.directory_uri(
            "team",
            "user",
            "session-abc",
            7,
        )

        self.assertRegex(merged_uri, r"conversation-[a-f0-9]{12}-000002-000004$")
        self.assertRegex(directory_uri, r"conversation-[a-f0-9]{12}/dir-007$")

    def test_write_directory_record_patches_keywords_and_fs(self) -> None:
        """Directory writes preserve add, keyword patch, and CortexFS order."""
        manager = self._make_manager()
        fs = MagicMock()
        fs.write_context = AsyncMock()
        manager._orchestrator._fs = fs
        service = RecompositionWriteService(manager)

        uri = self._run(
            service.write_directory_record(
                session_id="session-1",
                tenant_id="team",
                user_id="user",
                source_uri="source-uri",
                directory_index=1,
                segment={
                    "msg_range": [0, 3],
                    "source_records": [{"uri": "leaf-1"}, {"uri": "leaf-2"}],
                },
                children_abstracts=["child one", "child two"],
                derived={
                    "abstract": "summary",
                    "overview": "overview",
                    "keywords": ["alpha", "beta"],
                },
                aggregated_meta={"entities": ["Alice"]},
                all_tool_calls=[{"name": "tool"}],
            )
        )

        manager._orchestrator.add.assert_awaited_once()
        add_kwargs = manager._orchestrator.add.await_args.kwargs
        self.assertEqual(add_kwargs["uri"], uri)
        self.assertEqual(add_kwargs["content"], "child one\n\nchild two")
        self.assertEqual(add_kwargs["meta"]["layer"], "directory")
        self.assertEqual(add_kwargs["meta"]["child_uris"], ["leaf-1", "leaf-2"])
        manager._orchestrator._storage.update.assert_awaited_once_with(
            "context",
            "record-id",
            {"keywords": "alpha, beta"},
        )
        fs.write_context.assert_awaited_once()

    def test_write_session_summary_persists_summary_record(self) -> None:
        """Session summary writes preserve metadata and keyword patching."""
        manager = self._make_manager()
        service = RecompositionWriteService(manager)

        uri = self._run(
            service.write_session_summary(
                session_id="session-1",
                tenant_id="team",
                user_id="user",
                source_uri="source-uri",
                abstracts=["a", "b"],
                llm_abstract="summary",
                llm_overview="overview",
                keywords_list=["topic"],
            )
        )

        self.assertTrue(uri.endswith("/session/conversations/session-1/summary"))
        add_kwargs = manager._orchestrator.add.await_args.kwargs
        self.assertEqual(add_kwargs["uri"], uri)
        self.assertEqual(add_kwargs["meta"]["layer"], "session_summary")
        self.assertEqual(add_kwargs["meta"]["child_count"], 2)
        manager._orchestrator._storage.update.assert_awaited_once_with(
            "context",
            "record-id",
            {"keywords": "topic"},
        )

    def test_write_merged_leaf_tracks_deferred_derive(self) -> None:
        """Merged leaf writes schedule derive on the recomposition task service."""

        async def _exercise() -> MagicMock:
            manager = self._make_manager()
            service = RecompositionWriteService(manager)
            sk = ("context", "team", "user", "session-1")

            uri = await service.write_merged_leaf(
                sk=sk,
                session_id="session-1",
                tenant_id="team",
                user_id="user",
                source_uri="source-uri",
                msg_range=[0, 1],
                content="hello",
                aggregated_meta={"entities": ["Alice"]},
                all_tool_calls=[],
            )
            self.assertTrue(uri.endswith("000000-000001"))

            failures = await manager._recomposition_tasks.wait_for_merge_followup_tasks(
                sk
            )
            self.assertEqual(failures, [])
            return manager

        manager = self._run(_exercise())

        add_kwargs = manager._orchestrator.add.await_args.kwargs
        self.assertTrue(add_kwargs["defer_derive"])
        self.assertEqual(add_kwargs["meta"]["layer"], "merged")
        manager._orchestrator._complete_deferred_derive.assert_awaited_once()
        derive_kwargs = (
            manager._orchestrator._complete_deferred_derive.await_args.kwargs
        )
        self.assertEqual(derive_kwargs["content"], "hello")
        self.assertTrue(derive_kwargs["raise_on_error"])


if __name__ == "__main__":
    unittest.main()
