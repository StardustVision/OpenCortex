# SPDX-License-Identifier: Apache-2.0
"""Tests for recomposition state helpers."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Coroutine
from unittest.mock import AsyncMock, MagicMock

from opencortex.context.manager import ConversationBuffer
from opencortex.context.recomposition_state import RecompositionStateService
from opencortex.context.recomposition_tasks import ContextRecompositionTaskService


class TestRecompositionStateService(unittest.TestCase):
    """Lock buffer state and cleanup behavior behind the state service."""

    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run(coro)

    def _make_manager(self) -> MagicMock:
        manager = MagicMock()
        manager._conversation_buffers = {}
        manager._recomposition_tasks = ContextRecompositionTaskService(manager)
        manager._orchestrator._get_collection.return_value = "context"
        manager._orchestrator._storage.remove_by_uri = AsyncMock()
        manager._orchestrator._storage.filter = AsyncMock(return_value=[])
        manager._orchestrator._fs = None
        return manager

    def test_take_merge_snapshot_detaches_buffer(self) -> None:
        """Snapshot copies current buffer and advances the live start index."""
        manager = self._make_manager()
        manager._orchestrator._config.conversation_merge_token_budget = 10
        sk = ("context", "tenant", "user", "session")
        manager._conversation_buffers[sk] = ConversationBuffer(
            messages=["a", "b"],
            token_count=12,
            start_msg_index=4,
            immediate_uris=["u1", "u2"],
            tool_calls_per_turn=[[{"name": "tool"}], []],
        )
        service = RecompositionStateService(manager, MagicMock())

        snapshot = self._run(service.take_merge_snapshot(sk, flush_all=False))

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.messages, ["a", "b"])
        self.assertEqual(snapshot.start_msg_index, 4)
        live = manager._conversation_buffers[sk]
        self.assertEqual(live.messages, [])
        self.assertEqual(live.start_msg_index, 6)

    def test_restore_merge_snapshot_prepends_detached_snapshot(self) -> None:
        """Restore keeps failed snapshot before newer live messages."""
        manager = self._make_manager()
        sk = ("context", "tenant", "user", "session")
        snapshot = ConversationBuffer(
            messages=["old"],
            token_count=3,
            start_msg_index=1,
            immediate_uris=["u-old"],
            tool_calls_per_turn=[["old-tool"]],
        )
        manager._conversation_buffers[sk] = ConversationBuffer(
            messages=["new"],
            token_count=4,
            start_msg_index=2,
            immediate_uris=["u-new"],
            tool_calls_per_turn=[["new-tool"]],
        )
        service = RecompositionStateService(manager, MagicMock())

        self._run(service.restore_merge_snapshot(sk, snapshot))

        restored = manager._conversation_buffers[sk]
        self.assertEqual(restored.messages, ["old", "new"])
        self.assertEqual(restored.token_count, 7)
        self.assertEqual(restored.start_msg_index, 1)
        self.assertEqual(restored.immediate_uris, ["u-old", "u-new"])
        self.assertEqual(restored.tool_calls_per_turn, [["old-tool"], ["new-tool"]])

    def test_purge_deduplicates_uris_and_keeps_fs_best_effort(self) -> None:
        """Storage delete is authoritative while CortexFS failures are swallowed."""
        manager = self._make_manager()
        fs = MagicMock()
        fs.rm = AsyncMock(side_effect=[None, RuntimeError("disk")])
        manager._orchestrator._fs = fs
        service = RecompositionStateService(manager, MagicMock())

        self._run(service.purge_records_and_fs_subtree(["u1", "u1", "", "u2"]))

        self.assertEqual(
            manager._orchestrator._storage.remove_by_uri.await_args_list,
            [
                unittest.mock.call("context", "u1"),
                unittest.mock.call("context", "u2"),
            ],
        )
        self.assertEqual(fs.rm.await_count, 2)


if __name__ == "__main__":
    unittest.main()
